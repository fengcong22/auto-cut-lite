import asyncio
import importlib.util
import json
import os
import re
import ssl
from typing import Optional, Tuple

import websockets


def get_jy_local_config() -> Optional[Tuple[str, str]]:
    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        return None

    jy_user_data = os.path.join(local_app_data, r"JianyingPro\User Data")
    cfg: dict[str, Optional[str]] = {"device_id": None, "iid": None}

    ttnet_path = os.path.join(jy_user_data, r"TTNet\tt_net_config.config")
    if os.path.exists(ttnet_path):
        try:
            with open(ttnet_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            m = re.search(r"device_id&#\*(\d+)", content)
            if m:
                cfg["device_id"] = m.group(1)
        except Exception:
            pass

    log_dir = os.path.join(jy_user_data, "Log")
    if os.path.exists(log_dir):
        logs = sorted(
            [os.path.join(log_dir, x) for x in os.listdir(log_dir) if x.endswith(".log")],
            key=os.path.getmtime,
            reverse=True,
        )
        for p in logs[:5]:
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    chunk = f.read(1_000_000)
                m = re.search(r"iid=(\d+)", chunk)
                if m:
                    cfg["iid"] = m.group(1)
                    break
            except Exception:
                continue

    if cfg["device_id"] and cfg["iid"]:
        return cfg["device_id"], cfg["iid"]
    return None


APP_KEY = "IZjhUeAYwP"
APP_ID = "3704"


def _build_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _edge_tts_available() -> bool:
    try:
        return importlib.util.find_spec("edge_tts") is not None
    except (ImportError, ValueError):
        return False


def get_tts_status() -> dict[str, object]:
    try:
        identity_present = get_jy_local_config() is not None
    except OSError:
        identity_present = False
    edge_available = _edge_tts_available()
    return {
        "sami": {
            "status": "pending",
            "code": (
                "configuration_present_unverified"
                if identity_present
                else "requires_local_jianying"
            ),
            "target_local_identity_present": identity_present,
            "tls_verification": "required",
            "network_probe": "not_run",
        },
        "edge": {
            "status": "pending",
            "code": "external_service_unverified" if edge_available else "install_on_first_run",
            "package_importable": edge_available,
            "network_probe": "not_run",
        },
    }


def _redact_target_identity(message: object, dev_id: str, iid: str) -> str:
    redacted = str(message)
    for value in (dev_id, iid):
        if value:
            redacted = redacted.replace(value, "[redacted]")
    return redacted


async def _run_sami_tts(text: str, speaker: str, output_file: str, dev_id: str, iid: str):
    ws_url = f"wss://sami.bytedance.com/internal/api/v2/ws?device_id={dev_id}&iid={iid}"
    headers = {
        "User-Agent": f"JianyingPro/5.9.0.11632 (Windows 10.0.19045; app_id:3704; device_id:{dev_id})"
    }
    ssl_context = _build_ssl_context()

    try:
        async with websockets.connect(
            ws_url, additional_headers=headers, ssl=ssl_context, open_timeout=20
        ) as ws:
            task_id = f"ai_gen_{os.urandom(4).hex()}"
            start_msg = {
                "app_id": APP_ID,
                "appkey": APP_KEY,
                "event": "StartTask",
                "namespace": "TTS",
                "task_id": task_id,
                "message_id": task_id + "_0",
                "payload": json.dumps(
                    {
                        "text": text,
                        "speaker": speaker,
                        "audio_config": {
                            "format": "ogg_opus",
                            "sample_rate": 24000,
                            "bit_rate": 64000,
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
            await ws.send(json.dumps(start_msg, ensure_ascii=False, separators=(",", ":")))
            await ws.send(
                json.dumps({"appkey": APP_KEY, "event": "FinishTask", "namespace": "TTS"})
            )

            audio_data = bytearray()
            while True:
                try:
                    resp_raw = await asyncio.wait_for(ws.recv(), timeout=15)
                except asyncio.TimeoutError:
                    return False, "SAMI Timeout"

                if isinstance(resp_raw, str):
                    resp = json.loads(resp_raw)
                    event = resp.get("event")
                    if event == "TaskFailed":
                        return (
                            False,
                            _redact_target_identity(
                                f"SAMI Error: {resp.get('status_text')} "
                                f"(Code: {resp.get('status_code')})",
                                dev_id,
                                iid,
                            ),
                        )
                    if event == "TaskFinished":
                        break
                else:
                    audio_data.extend(resp_raw)

            if audio_data:
                with open(output_file, "wb") as f:
                    f.write(audio_data)
                return True, output_file
            return False, "No audio"
    except Exception as e:
        return False, _redact_target_identity(e, dev_id, iid)


async def _run_edge_tts(text: str, output_file: str, voice: str = "zh-CN-YunxiNeural"):
    try:
        import edge_tts

        actual_path = output_file
        if output_file.endswith(".ogg") and not output_file.endswith(".mp3"):
            actual_path = output_file + ".mp3"

        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(actual_path)
        return True, actual_path
    except Exception as e:
        return False, f"Edge-TTS Error: {str(e)}"


async def generate_voice_with_meta(
    text: str,
    output_path: str,
    speaker: str = "zh_male_huoli",
    *,
    backend: Optional[str] = None,
    allow_fallback: bool = True,
    sami_retries: int = 2,
) -> Tuple[Optional[str], Optional[str]]:
    """
    backend: None | "sami" | "edge"
    allow_fallback: when True, SAMI failure may fallback to edge.
    returns: (audio_path, backend_used)
    """
    force_sami = backend == "sami"
    force_edge = backend == "edge"
    identity = None
    if not force_edge:
        try:
            identity = get_jy_local_config()
        except OSError:
            pass

    if not force_edge and identity is not None:
        dev_id, iid = identity
        attempts = max(1, int(sami_retries))
        print("[*] Intelligent TTS: backend=sami identity=target_local_present", flush=True)
        for i in range(attempts):
            ok, res = await _run_sami_tts(text, speaker, output_path, dev_id, iid)
            if ok:
                print("[+] Intelligent TTS: backend=sami status=success", flush=True)
                return res, "sami"
            print(f"[!] Intelligent TTS: backend=sami status=failed attempt={i + 1}/{attempts}")
            if i + 1 < attempts:
                await asyncio.sleep(0.35)

        if force_sami or not allow_fallback:
            return None, None
    elif not force_edge:
        print("[!] Intelligent TTS: backend=sami identity=target_local_missing", flush=True)
        if force_sami or not allow_fallback:
            return None, None

    voice = "zh-CN-YunxiNeural" if "male" in speaker else "zh-CN-XiaoxiaoNeural"
    ok_edge, res_edge = await _run_edge_tts(text, output_path, voice)
    if ok_edge:
        print("[+] Intelligent TTS: backend=edge status=success", flush=True)
        return res_edge, "edge"
    return None, None


async def generate_voice(
    text: str,
    output_path: str,
    speaker: str = "zh_male_huoli",
    *,
    backend: Optional[str] = None,
    allow_fallback: bool = True,
    sami_retries: int = 2,
):
    path, _backend_used = await generate_voice_with_meta(
        text,
        output_path,
        speaker,
        backend=backend,
        allow_fallback=allow_fallback,
        sami_retries=sami_retries,
    )
    return path


if __name__ == "__main__":
    asyncio.run(generate_voice("测试智能配音系统集成成功。", "test.ogg"))
