"""R-key changer のエントリポイント。

実行方法::

    pip install -r requirements.txt
    python main.py

このアプリは「再生中の音をキー変更して聴く」機能だけの単体版です。
パソコンで鳴っている音を、その場で半音単位に移調して聴きます。
録音・保存は一切しません。
"""

from __future__ import annotations

import sys
from pathlib import Path

# スクリプトのあるフォルダを import パスに加える
# （どのカレントディレクトリから起動しても動くようにするため）
_BASE_DIR = Path(__file__).resolve().parent
if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))


def main() -> int:
    """キー変更ウィンドウを単体で起動する。

    Returns:
        終了コード。正常終了なら 0。
    """
    try:
        import customtkinter as ctk
    except ImportError as exc:
        print(f"必要なライブラリが不足しています: {exc}", file=sys.stderr)
        print("  pip install -r requirements.txt", file=sys.stderr)
        return 1

    from audio.app_settings import load_settings
    from gui.live_monitor_window import LiveMonitorWindow

    ctk.set_appearance_mode("dark")

    # 見えないルートウィンドウを1枚だけ作り、キー変更画面をその子として
    # 開く。LiveMonitorWindow は元々「アプリ本体の一部として開く別
    # ウィンドウ」として作られており、``master.settings`` だけを読む
    # ので、ここに settings を持たせれば無改造のまま流用できる。
    root = ctk.CTk()
    root.withdraw()
    root.settings = load_settings()

    window = LiveMonitorWindow(root)

    def on_window_destroy(event: object) -> None:
        # ウィンドウ自身の破棄イベントだけを見る（子ウィジェットの
        # 破棄でも <Destroy> は伝播するため）。
        if getattr(event, "widget", None) is window:
            root.destroy()

    window.bind("<Destroy>", on_window_destroy)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
