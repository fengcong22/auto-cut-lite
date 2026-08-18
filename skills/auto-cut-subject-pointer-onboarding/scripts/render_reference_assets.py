from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from audio_sound.config import configure_playwright_browser_path  # noqa: E402

OUTPUT_DIRECTORY = Path(__file__).resolve().parents[1] / "assets"
VIEWPORT = {"width": 1600, "height": 900}


COMMON_CSS = """
  * { box-sizing: border-box; }
  html, body { margin: 0; width: 1600px; height: 900px; overflow: hidden; }
  body {
    font-family: Microsoft YaHei, Noto Sans SC, sans-serif;
    color: #17212b;
    background: #f4f6f8;
    letter-spacing: 0;
  }
  #canvas {
    width: 1600px;
    height: 900px;
    padding: 54px 64px 48px;
    background: #f4f6f8;
    position: relative;
  }
  .topline { display: flex; align-items: flex-start; justify-content: space-between; }
  h1 { margin: 8px 0 0; font-size: 42px; line-height: 1.2; font-weight: 750; }
  .eyebrow { font-size: 16px; color: #087f8c; font-weight: 700; }
  .badge {
    border: 2px solid #c4362d;
    color: #9f251f;
    background: #fff;
    padding: 10px 15px;
    border-radius: 4px;
    font-size: 16px;
    font-weight: 700;
  }
  .content { display: grid; grid-template-columns: minmax(0, 1fr) 420px; gap: 36px; margin-top: 38px; }
  .panel { background: #fff; border: 1px solid #cbd3da; border-radius: 6px; }
  .checklist { padding: 28px 30px; }
  .checklist h2 { margin: 0 0 22px; font-size: 22px; }
  .check {
    display: grid;
    grid-template-columns: 28px 1fr;
    gap: 12px;
    align-items: start;
    padding: 15px 0;
    border-top: 1px solid #e1e6ea;
    font-size: 20px;
    line-height: 1.42;
  }
  .check:first-of-type { border-top: 0; }
  .mark {
    width: 24px; height: 24px; border-radius: 50%;
    background: #087f8c; color: #fff; text-align: center;
    font-size: 16px; line-height: 24px; font-weight: 800;
  }
  .footer {
    position: absolute; left: 64px; right: 64px; bottom: 28px;
    display: flex; justify-content: space-between; align-items: center;
    color: #65717c; font-size: 15px;
  }
  .footer strong { color: #9f251f; }
"""


MATERIAL_HTML = f"""
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
{COMMON_CSS}
  .material-stage {{
    height: 584px; padding: 28px; position: relative; overflow: hidden;
    background-color: #fff;
    background-image:
      linear-gradient(45deg, #e9edf0 25%, transparent 25%),
      linear-gradient(-45deg, #e9edf0 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, #e9edf0 75%),
      linear-gradient(-45deg, transparent 75%, #e9edf0 75%);
    background-size: 36px 36px;
    background-position: 0 0, 0 18px, 18px -18px, -18px 0;
  }}
  .asset-boundary {{
    position: absolute; left: 188px; top: 96px; width: 540px; height: 370px;
    border: 3px dashed #087f8c; background: rgba(255,255,255,.8);
  }}
  .asset-label {{ position: absolute; left: 18px; top: 16px; color: #087f8c; font-weight: 700; }}
  .shape-a {{ position: absolute; left: 166px; top: 94px; width: 190px; height: 190px; border: 24px solid #17212b; border-radius: 50%; }}
  .shape-b {{ position: absolute; left: 330px; top: 238px; width: 120px; height: 72px; background: #f0b429; border: 5px solid #17212b; border-radius: 3px; }}
  .shape-note {{ position: absolute; left: 164px; bottom: 28px; font-size: 17px; color: #65717c; }}
</style></head><body>
<main id="canvas">
  <div class="topline">
    <div><div class="eyebrow">AUTO-CUT / 素材交付参考</div><h1>学科指向物 PNG 提供样式</h1></div>
    <div class="badge">非生产示意 / NON-PRODUCTION</div>
  </div>
  <section class="content">
    <div class="panel material-stage" aria-label="透明背景素材示意">
      <div class="asset-boundary">
        <div class="asset-label">完整素材边界</div>
        <div class="shape-a"></div><div class="shape-b"></div>
        <div class="shape-note">中性几何占位形状，不是实际学科指向物</div>
      </div>
    </div>
    <aside class="panel checklist">
      <h2>提供前检查</h2>
      <div class="check"><span class="mark">✓</span><span>透明背景</span></div>
      <div class="check"><span class="mark">✓</span><span>完整边缘</span></div>
      <div class="check"><span class="mark">✓</span><span>一种素材一个文件</span></div>
      <div class="check"><span class="mark">✓</span><span>保留原始分辨率</span></div>
      <div class="check"><span class="mark">✓</span><span>不要提供截图裁切出的素材</span></div>
    </aside>
  </section>
  <footer class="footer"><span>画布规格 1600 × 900</span><strong>仅说明文件交付方式，不可投入成片</strong></footer>
</main></body></html>
"""


SCALE_HTML = f"""
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
{COMMON_CSS}
  .content {{ grid-template-columns: minmax(0, 1fr) 390px; gap: 32px; margin-top: 30px; }}
  .frame-wrap {{ padding: 20px; }}
  .video-frame {{
    width: 100%; aspect-ratio: 16 / 9; border: 4px solid #17212b;
    background: #e8eef1; position: relative; overflow: hidden;
  }}
  .lesson-bar {{ height: 66px; background: #fff; border-bottom: 2px solid #cbd3da; padding: 18px 24px; font-size: 18px; font-weight: 700; }}
  .board {{ position: absolute; left: 44px; right: 44px; top: 100px; bottom: 52px; background: #fff; border: 1px solid #aeb9c2; }}
  .board-title {{ margin: 26px 30px; font-size: 26px; font-weight: 750; }}
  .line {{ height: 16px; margin: 18px 30px; background: #d8dfe4; border-radius: 2px; }}
  .line.short {{ width: 44%; }} .line.mid {{ width: 68%; }}
  .target {{ position: absolute; right: 124px; top: 205px; width: 164px; height: 92px; border: 4px solid #c4362d; background: #fff4d6; text-align: center; line-height: 84px; font-size: 18px; font-weight: 700; }}
  .placeholder {{ position: absolute; right: 306px; top: 214px; width: 74px; height: 74px; background: #087f8c; border: 5px solid #17212b; color: #fff; font-size: 13px; display: grid; place-items: center; text-align: center; }}
  .measure {{ position: absolute; right: 300px; top: 302px; width: 86px; text-align: center; color: #087f8c; font-size: 14px; font-weight: 700; }}
  .meta {{ display: grid; grid-template-columns: 1.25fr 1fr 1.35fr; gap: 10px; margin-top: 16px; }}
  .meta span {{ border: 1px solid #cbd3da; background: #fff; border-radius: 3px; padding: 12px 14px; font-size: 16px; }}
  .checklist {{ padding: 22px 28px; }} .checklist h2 {{ margin-bottom: 12px; }}
  .check {{ font-size: 18px; padding: 12px 0; }}
</style></head><body>
<main id="canvas">
  <div class="topline">
    <div><div class="eyebrow">AUTO-CUT / 比例证据参考</div><h1>大小比例参考截图样式</h1></div>
    <div class="badge">非生产示意 / NON-PRODUCTION</div>
  </div>
  <section class="content">
    <div class="panel frame-wrap">
      <div class="video-frame">
        <div class="lesson-bar">完整课程画面示意</div>
        <div class="board"><div class="board-title">课程内容与版式必须完整保留</div><div class="line mid"></div><div class="line short"></div><div class="target">目标区域</div><div class="placeholder">素材<br>占位</div><div class="measure">正确比例</div></div>
      </div>
      <div class="meta"><span>课节：示例课 03</span><span>时间点：00:12:36</span><span>画面类型：完整课件画面</span></div>
    </div>
    <aside class="panel checklist">
      <h2>截图必须满足</h2>
      <div class="check"><span class="mark">✓</span><span>保留完整 16:9 画面</span></div>
      <div class="check"><span class="mark">✓</span><span>指向物和目标同时可见</span></div>
      <div class="check"><span class="mark">✓</span><span>当前显示大小必须是正确比例</span></div>
      <div class="check"><span class="mark">✓</span><span>标明：课节 / 时间点 / 画面类型</span></div>
      <div class="check"><span class="mark">✓</span><span>不要只截局部预览区域</span></div>
    </aside>
  </section>
  <footer class="footer"><span>画布规格 1600 × 900</span><strong>中性占位仅说明截图范围与标注方式</strong></footer>
</main></body></html>
"""


def render_card(html: str, output_path: Path) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    configure_playwright_browser_path(REPO_ROOT)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
            page.set_content(html, wait_until="load")
            page.emulate_media(reduced_motion="reduce")
            page.locator("#canvas").screenshot(
                path=str(output_path),
                animations="disabled",
                type="png",
            )
        finally:
            browser.close()


def render_reference_assets(output_directory: Path = OUTPUT_DIRECTORY) -> list[Path]:
    guides = (
        ("pointer-material-reference.png", MATERIAL_HTML),
        ("scale-reference-screenshot.png", SCALE_HTML),
    )
    rendered: list[Path] = []
    for filename, html in guides:
        output_path = output_directory / filename
        render_card(html, output_path)
        rendered.append(output_path)
    return rendered


def main() -> int:
    for path in render_reference_assets():
        print(path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
