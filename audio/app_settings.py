"""アプリの設定（1つだけ）を読み書きする。

このアプリで唯一覚えておく必要があるのは「キー変更画面の手順パネルを
畳んだかどうか」だけ（一度接続に成功したら、次回以降は手順を畳んで
おく。:class:`gui.live_monitor_window.LiveMonitorWindow` 参照）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import config

_SETTINGS_FILE_NAME = "app_settings.json"


@dataclass
class AppSettings:
    """アプリの設定。"""

    live_monitor_guide_seen: bool = False
    """キー変更画面の手順パネルを一度でも開始（接続成功）まで進めたか。"""

    def to_dict(self) -> dict[str, Any]:
        """辞書に変換する。

        Returns:
            JSON へ書き出せる辞書。
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppSettings:
        """辞書から復元する。未知のキーは無視し、欠けたキーは既定値で補う。

        Args:
            data: :meth:`to_dict` が返した辞書。

        Returns:
            復元された設定。
        """
        defaults = cls()
        return cls(
            live_monitor_guide_seen=bool(
                data.get("live_monitor_guide_seen", defaults.live_monitor_guide_seen)
            )
        )


def _settings_path() -> Path:
    """設定ファイルの保存先パスを返す。

    Returns:
        アプリのフォルダ直下の固定ファイルパス。
    """
    return config.BASE_DIR / _SETTINGS_FILE_NAME


def load_settings() -> AppSettings:
    """保存済みの設定を読み込む。無ければ既定値を返す。

    Returns:
        読み込んだ（または既定の）設定。壊れたファイルでも例外は
        投げず、既定値へフォールバックする。
    """
    path = _settings_path()
    if not path.exists():
        return AppSettings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppSettings()
    return AppSettings.from_dict(data)


def save_settings(settings: AppSettings) -> None:
    """設定をファイルへ保存する。

    Args:
        settings: 保存する設定。

    失敗しても例外は投げない（設定保存はあくまで利便性のためのもので、
    ここで落ちてアプリ本体が使えなくなる方が困る）。
    """
    path = _settings_path()
    try:
        path.write_text(
            json.dumps(settings.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
