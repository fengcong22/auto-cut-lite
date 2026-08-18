import argparse
import csv
import ipaddress
import os
import re
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse

import requests

try:
    from scripts.utils.config import CONFIG
    from scripts.utils.logging_utils import setup_logger
except ModuleNotFoundError:  # Direct execution from scripts/.
    from utils.config import CONFIG
    from utils.logging_utils import setup_logger

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_DOWNLOAD_BYTES = int(CONFIG.cloud_max_mb * 1024 * 1024)
ALLOWED_SCHEMES = {"http", "https"}
logger = setup_logger("cloud_manager")


class CloudManager:
    def __init__(self, repo_root: Path | None = None, requester=requests):
        self.repo_root = Path(repo_root or DEFAULT_REPO_ROOT).resolve()
        self.data_dir = self.repo_root / "data"
        self.cache_dir = self.repo_root / "tmp" / "cloud_cache"
        self.capture_dir = self.repo_root / "tmp" / "capture"
        self.requester = requester
        self.index_rows: list[dict] = []
        self.assets = self._load_database()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _load_database(self) -> Dict[str, dict]:
        assets: Dict[str, dict] = {}
        db_files = ["cloud_music_library.csv", "cloud_video_assets.csv", "cloud_sound_effects.csv"]

        for db_name in db_files:
            path = self.data_dir / db_name
            if not path.exists():
                continue

            try:
                with path.open("r", encoding="utf-8") as f:
                    lines = [line for line in f.readlines() if not line.startswith("#")]
                    reader = csv.DictReader(lines)
                    for row in reader:
                        eid = row.get("id") or row.get("music_id") or row.get("effect_id")
                        if not eid:
                            continue
                        name = row.get("name") or row.get("title") or row.get("name_hint") or ""
                        dur = row.get("duration_s") or row.get("duration")
                        asset = {
                            "id": str(eid),
                            "name": str(name),
                            "url": row.get("url", ""),
                            "duration_s": (
                                float(dur)
                                if dur and str(dur).replace(".", "", 1).isdigit()
                                else None
                            ),
                            "type": row.get("type") or row.get("categories", "unknown"),
                            "source_db": db_name,
                        }
                        self.index_rows.append(asset)
                        assets[str(eid)] = asset
            except Exception as e:
                logger.warning("Error loading %s: %s", db_name, e)

        if assets:
            logger.info("Cloud Manager indexed %d items.", len(assets))
        return assets

    def find_asset(self, query: str) -> Optional[dict]:
        """
        Find by ID or fuzzy name.
        Important rule: rows without URL are treated as unavailable and not returned.
        """
        if query in self.assets:
            asset = self.assets[query]
            return asset if asset.get("url") else None

        q = str(query).lower()
        for asset in self.assets.values():
            if not asset.get("url"):
                continue
            if q in str(asset.get("name", "")).lower():
                return asset
        return None

    def get_asset_duration(self, query: str) -> Optional[float]:
        asset = self.find_asset(query)
        if asset:
            return asset.get("duration_s")
        return None

    def get_url_from_logs(self, effect_id: str) -> Optional[str]:
        log_files = [
            self.capture_dir / "mitmdump_assets_capture.log",
            self.capture_dir / "mitmdump_media_full.log",
        ]

        for log_path in log_files:
            if not log_path.exists():
                continue
            with log_path.open("r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            id_pattern = f'"(?:effect_id|id)":"{effect_id}"'
            matches = list(re.finditer(id_pattern, content))
            if not matches:
                continue

            for m in reversed(matches):
                region = content[m.end() : m.end() + 10000]
                url_match = re.search(
                    r'https?://[^\s"\'\]]+(?:\.mp4|\.webm|\.zip|\.7z|a=4066)[^\s"\'\]]*',
                    region,
                    re.IGNORECASE,
                )
                if url_match:
                    return url_match.group(0).replace("\\u0026", "&").replace("\\/", "/")
        return None

    def _is_safe_download_url(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
            if parsed.scheme.lower() not in ALLOWED_SCHEMES:
                return False
            host = (parsed.hostname or "").strip().lower()
            if not host:
                return False
            if host in {"localhost", "127.0.0.1", "::1"}:
                return False
            try:
                ip = ipaddress.ip_address(host)
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    return False
            except ValueError:
                pass
            return True
        except Exception:
            return False

    def _validate_response_headers(self, res: requests.Response) -> bool:
        content_type = (res.headers.get("Content-Type") or "").lower()
        if content_type.startswith("text/html") or content_type.startswith("application/json"):
            return False
        if content_type and not (
            "video/" in content_type
            or "audio/" in content_type
            or "application/octet-stream" in content_type
            or "application/zip" in content_type
            or "binary/octet-stream" in content_type
        ):
            return False
        content_length = res.headers.get("Content-Length")
        if content_length and content_length.isdigit() and int(content_length) > MAX_DOWNLOAD_BYTES:
            return False
        return True

    def _is_audio_asset(self, asset: dict) -> bool:
        source_db = str(asset.get("source_db", "")).lower()
        db_type = str(asset.get("type", "")).lower()
        if source_db in {"cloud_music_library.csv", "cloud_sound_effects.csv"}:
            return True
        return any(k in db_type for k in ["music", "audio", "sound", "bgm", "音效", "歌曲", "歌"])

    def _infer_extension(self, asset: dict, url: str, content_type: str = "") -> str:
        mime_type_hint = ""
        try:
            parsed = urlparse(url)
            mime_type_hint = (parse_qs(parsed.query).get("mime_type", [""])[0] or "").lower()
        except Exception:
            mime_type_hint = ""

        content_type = (content_type or "").lower()
        is_audio = self._is_audio_asset(asset)

        if "audio" in mime_type_hint or content_type.startswith("audio/"):
            if "mpeg" in mime_type_hint or "mpeg" in content_type:
                return ".mp3"
            if "wav" in mime_type_hint or "wav" in content_type:
                return ".wav"
            if "ogg" in mime_type_hint or "ogg" in content_type:
                return ".ogg"
            return ".m4a"

        if "video" in mime_type_hint or content_type.startswith("video/"):
            return ".mp4"

        if is_audio:
            return ".m4a"
        return ".mp4"

    def download_asset(self, query: str, force: bool = False) -> Optional[str]:
        asset = self.find_asset(query)
        if not asset:
            logger.warning("Cloud Asset '%s' not found in database.", query)
            return None

        eid = asset["id"]
        safe_name = "".join([c for c in asset["name"] if c.isalnum() or c in (" ", "_")]).strip()

        url = asset.get("url")
        if (not url) or force:
            url = self.get_url_from_logs(eid)

        if not url:
            logger.warning("No valid download URL found for ID %s.", eid)
            return None
        if not self._is_safe_download_url(url):
            logger.warning("Unsafe download URL blocked for ID %s: %s", eid, url)
            return None

        ext = self._infer_extension(asset, url=url)
        local_filename = f"{eid}_{safe_name}{ext}"
        local_path = self.cache_dir / local_filename
        legacy_mp4_path = self.cache_dir / f"{eid}_{safe_name}.mp4"

        if not force:
            if local_path.exists():
                return str(local_path)
            if ext != ".mp4" and legacy_mp4_path.exists():
                try:
                    os.replace(legacy_mp4_path, local_path)
                    return str(local_path)
                except Exception:
                    return str(legacy_mp4_path)

        logger.info("Downloading Cloud Asset: %s", asset["name"])
        partial_paths = {Path(f"{local_path}.part")}
        try:
            res = self.requester.get(url, stream=True, timeout=60)
            res.raise_for_status()
            if not self._validate_response_headers(res):
                logger.warning("Download blocked by header validation for ID %s.", eid)
                return None

            ext_from_headers = self._infer_extension(
                asset, url=url, content_type=(res.headers.get("Content-Type") or "")
            )
            if ext_from_headers != ext:
                ext = ext_from_headers
                local_filename = f"{eid}_{safe_name}{ext}"
                local_path = self.cache_dir / local_filename

            tmp_path = Path(f"{local_path}.part")
            partial_paths.add(tmp_path)
            total = 0
            with tmp_path.open("wb") as f:
                for chunk in res.iter_content(chunk_size=32768):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError(f"Download exceeds size limit: {MAX_DOWNLOAD_BYTES} bytes")
                    f.write(chunk)
            os.replace(tmp_path, local_path)
            logger.info("Download finished: %s", local_path)
            return str(local_path)
        except Exception as e:
            logger.error("Download error: %s", e)
            return None
        finally:
            for part in partial_paths:
                try:
                    part.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Could not remove partial download %s: %s", part.name, exc)


def cloud_material_status(repo_root: Path) -> dict[str, object]:
    manager = CloudManager(repo_root=repo_root)
    indexed = len(manager.index_rows)
    downloadable = sum(
        1
        for asset in manager.index_rows
        if asset.get("url") and manager._is_safe_download_url(str(asset["url"]))
    )
    if indexed == 0:
        status = "unavailable"
        code = "cloud_index_empty"
    else:
        status = "degraded"
        code = "static_index_ready_remote_unverified"
    return {
        "status": status,
        "code": code,
        "indexed": indexed,
        "downloadable": downloadable,
        "unavailable": indexed - downloadable,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JianYing Cloud Asset Manager")
    parser.add_argument("query", help="ID or Name of the asset")
    parser.add_argument("--force", action="store_true", help="Force redownload")
    args = parser.parse_args()

    manager = CloudManager()
    path = manager.download_asset(args.query, args.force)
    if path:
        print(f"RESULT_PATH|{path}")
