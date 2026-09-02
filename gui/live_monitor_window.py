"""再生中の音をキー変更して聴く画面（🎚）。

:class:`audio.live_monitor.LiveMonitor` の操作パネル。仕組みそのものは
そちらのモジュール docstring を参照。この画面は

* VB-CABLE の配線手順を案内する
* 入力（横取り元）と出力（実際に鳴らす先）を選ばせる
* キーを ±12 半音で動かす
* 動いているかどうかを表示する

だけを受け持ち、音の処理には一切関わらない。

■ なぜ手順の案内を画面に置くのか

この機能は**アプリの外（Windows のサウンド設定）を先に直さないと
音が出ない**。手順書を別ファイルにすると、音が出ない人が必ず
「アプリの不具合」だと判断してしまうため、操作する場所のすぐ上に
手順を並べてある。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import Any

import customtkinter as ctk

import config
from audio import default_device
from audio.app_settings import save_settings
from audio.live_monitor import (
    KEY_SHIFT_MAX,
    KEY_SHIFT_MIN,
    LiveMonitor,
    LiveMonitorError,
    list_input_devices,
    list_output_devices,
    suggest_input_device,
    suggest_output_device,
)
from error_codes import with_code
from gui import theme

_POLL_MS = 500
"""状態表示を更新する間隔[ミリ秒]。音の処理とは無関係の見た目だけの周期。"""

_SETUP_STEPS: tuple[tuple[str, str], ...] = (
    (
        "① VB-CABLE を入れる",
        "vb-audio.com の「VB-CABLE Virtual Audio Device」を入れて"
        "パソコンを再起動します（無償。1 回だけ）。",
    ),
    (
        "② 聴きたい音楽を再生する",
        "Apple Music・YouTube など、いつも通り再生するだけです。"
        "ファイルの保存やダウンロードは行いません。",
    ),
    (
        "③ 下でデバイスを選ぶ",
        "入力に CABLE Output、出力に実際のヘッドホン／スピーカー。"
        "出力に CABLE Input を選ぶと音が回るので選べません。",
    ),
    (
        "④ 開始を押してキーを動かす",
        "終わったら「停止」を押すか画面を閉じれば、既定の出力は"
        "自動で元に戻ります。",
    ),
)
"""画面上部に並べる手順。``(見出し, 説明)`` の並び。

**「既定の出力を CABLE Input にする」という手順が無いのは、この画面を
開いた時点でアプリが自動で切り替えるため**（:meth:`_auto_switch_to_cable`
参照）。``comtypes``/Windows が使えない環境など、自動切り替えに失敗した
場合だけ :meth:`_build_guide` が手動手順を追加で表示する。
"""

_MANUAL_STEP_2 = (
    "（自動切り替えに失敗した場合）既定の出力を CABLE Input にする",
    "設定 ＞ システム ＞ サウンド の「出力」を手動で CABLE Input に"
    "変えてください。ここでスピーカーから音が消えるのが正常です。",
)
"""自動切り替えができなかったときだけ :data:`_SETUP_STEPS` へ追加する手順。"""

_NO_DEVICE = "（見つかりません）"
"""デバイスが 1 つも無いときにメニューへ出す文字列。"""


class LiveMonitorWindow(ctk.CTkToplevel):
    """再生中の音をキー変更して聴くためのウィンドウ。

    Attributes:
        monitor: 音を横取りして鳴らし直す本体。
    """

    def __init__(self, master: Any) -> None:
        """LiveMonitorWindow を初期化する。

        Args:
            master: 親ウィンドウ。
        """
        super().__init__(master)

        self.monitor = LiveMonitor()

        self._original_output_name = default_device.get_default_output_name()
        """開いた時点の既定出力デバイス名。停止・終了時に、既定が
        まだ CABLE 系のままなら元へ戻すのに使う
        （:meth:`_restore_default_output` 参照）。取得できなくても
        None のまま進む（この機能自体が任意の利便性用途のため）。

        **CABLE Input へ自動切り替えする前に取得すること。** 切り替えた
        後に取得すると「元の値」が CABLE Input になってしまい、
        戻し先が分からなくなる。
        """
        self._auto_switch_succeeded = self._auto_switch_to_cable()

        self._settings = master.settings
        """設定の実体（:class:`gui.main_window.AnalyzerFrame` が持つもの）を
        直接参照する。手順パネルを畳んだ状態を次回起動へ引き継ぐため
        （:attr:`_guide_collapsed` 参照）。
        """
        self._guide_collapsed = self._settings.live_monitor_guide_seen

        self._font_title = ctk.CTkFont(family=config.APP_FONT, size=15, weight="bold")
        self._font_label = ctk.CTkFont(family=config.APP_FONT, size=13)
        self._font_small = ctk.CTkFont(family=config.APP_FONT, size=11)
        self._font_eyebrow = ctk.CTkFont(
            family=config.APP_FONT, size=11, weight="bold"
        )
        self._font_key = ctk.CTkFont(family=theme.FONT_NUM, size=34, weight="bold")

        self._input_devices: list[dict[str, Any]] = []
        self._output_devices: list[dict[str, Any]] = []
        self._poll_id: str | None = None

        self.title("key changer")
        self.geometry("640x680")
        self.minsize(560, 600)
        self.configure(fg_color=theme.BG_DEEP)
        # 設定画面と同じ理由で transient は付けない（□ を残すため）

        self.grid_columnconfigure(0, weight=1)

        self._build_header()
        self._build_guide()
        self._build_device_panel()
        self._build_key_panel()
        self._build_control_row()

        self.refresh_devices()
        self._update_state_label()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._schedule_poll()

        # **既定の出力デバイスを切り替える COM 呼び出し（上の
        # _auto_switch_to_cable）は、Windows 側で「音声デバイスが
        # 変わった」イベントとして扱われ、まれにこのウィンドウが背面へ
        # 回ってしまうことがある（実機で確認済み）。作り終えた直後に
        # 明示的に前面へ出しておく。
        theme.bring_to_front(self)

    # ------------------------------------------------------------------
    #  組み立て
    # ------------------------------------------------------------------
    def _build_header(self) -> None:
        """見出しの帯を作る。"""
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 0))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text="Key changer",
            font=self._font_title,
            text_color=theme.TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")


        rule = theme.AccentRule(header, colors=(theme.VIOLET, theme.CYAN))
        rule.grid(row=2, column=0, sticky="ew", pady=(8, 0))

    def _build_guide(self) -> None:
        """VB-CABLE の配線手順を並べる。

        既定出力への自動切り替え（:meth:`_auto_switch_to_cable`）が
        成功していれば、その手順自体をリストから省く（もう終わっている
        ことを案内しても混乱を招くだけのため）。失敗していた場合だけ
        :data:`_MANUAL_STEP_2` を手動手順として差し込む。
        """
        panel = ctk.CTkFrame(
            self, fg_color=theme.PANEL, corner_radius=12, border_width=0
        )
        panel.grid(row=1, column=0, sticky="ew", padx=20, pady=(12, 0))
        panel.grid_columnconfigure(0, weight=1)

        # 見出し行。手順そのもの（self._guide_body）とは別の行に置き、
        # ここだけは折りたたんでも常に見えるようにする。接続状態の表示
        # （緑=デバイス接続中／赤=デバイスの接続に失敗）もここに置く。
        head = ctk.CTkFrame(panel, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 6))
        head.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            head,
            text="接続確認",
            font=self._font_eyebrow,
            text_color=theme.MUTED,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        self._guide_status_label = ctk.CTkLabel(
            head,
            text="",
            font=self._font_small,
            text_color=theme.MUTED,
            anchor="e",
        )
        self._guide_status_label.grid(row=0, column=1, sticky="e", padx=(8, 8))

        self._guide_toggle_button = ctk.CTkButton(
            head,
            text="▲",
            font=self._font_small,
            width=28,
            height=22,
            fg_color="transparent",
            border_width=1,
            command=self._on_toggle_guide,
        )
        self._guide_toggle_button.grid(row=0, column=2, sticky="e")
        theme.style_ghost_button(self._guide_toggle_button, theme.MUTED)

        body = ctk.CTkFrame(panel, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew")
        body.grid_columnconfigure(1, weight=1)
        self._guide_body = body

        steps = list(_SETUP_STEPS)
        if not self._auto_switch_succeeded:
            steps.insert(1, _MANUAL_STEP_2)

        row_index = 0
        if self._auto_switch_succeeded:
            ctk.CTkLabel(
                body,
                text="✓ 既定の出力は自動でCABLE Inputへ切り替えました",
                font=self._font_small,
                text_color=theme.GREEN,
                anchor="w",
            ).grid(row=row_index, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 6))
            row_index += 1

        for title, description in steps:
            ctk.CTkLabel(
                body,
                text=title,
                font=self._font_label,
                text_color=theme.TEXT_DIM,
                anchor="w",
            ).grid(row=row_index, column=0, sticky="nw", padx=(14, 10), pady=(0, 4))
            ctk.CTkLabel(
                body,
                text=description,
                font=self._font_small,
                text_color=theme.MUTED,
                anchor="w",
                justify="left",
                wraplength=340,
            ).grid(row=row_index, column=1, sticky="w", padx=(0, 14), pady=(0, 4))
            row_index += 1

        ctk.CTkLabel(
            body,
            text="",
            height=4,
        ).grid(row=row_index, column=0)

        # 前回すでに手順を読み終えている（一度でも接続に成功している）
        # 場合は、最初から畳んだ状態で表示する。まだ Toplevel が画面に
        # 出ていない段階では winfo_ismapped() が信用できない
        # （ヘッドレス環境で毎回確認済みの罠）ため、実際の表示状態は
        # :attr:`_guide_collapsed` というアプリ側の変数だけで管理する。
        self._apply_guide_collapsed()

    def _on_toggle_guide(self) -> None:
        """手順パネルの本体（self._guide_body）を隠す/出す。"""
        self._guide_collapsed = not self._guide_collapsed
        self._apply_guide_collapsed()

    def _apply_guide_collapsed(self) -> None:
        """:attr:`_guide_collapsed` の値に画面を合わせる。"""
        if self._guide_collapsed:
            self._guide_body.grid_remove()
            self._guide_toggle_button.configure(text="▼")
        else:
            self._guide_body.grid()
            self._guide_toggle_button.configure(text="▲")

    def _build_device_panel(self) -> None:
        """入力・出力デバイスの選択行を作る。"""
        panel = ctk.CTkFrame(self, fg_color="transparent")
        panel.grid(row=2, column=0, sticky="ew", padx=20, pady=(14, 0))
        panel.grid_columnconfigure(1, weight=1)

        self._input_var = tk.StringVar(value=_NO_DEVICE)
        self._output_var = tk.StringVar(value=_NO_DEVICE)
        self._input_menu = self._add_device_row(
            panel, row=0, text="入力", variable=self._input_var
        )
        self._output_menu = self._add_device_row(
            panel, row=1, text="出力", variable=self._output_var
        )

        self._refresh_button = ctk.CTkButton(
            panel,
            text="一覧を更新",
            font=self._font_small,
            width=100,
            height=28,
            command=self.refresh_devices,
        )
        self._refresh_button.grid(row=2, column=1, sticky="e", pady=(8, 0))
        theme.style_ghost_button(self._refresh_button, theme.MUTED)

    def _add_device_row(
        self, parent: Any, row: int, text: str, variable: tk.StringVar
    ) -> ctk.CTkOptionMenu:
        """デバイス選択の 1 行を作る。

        Args:
            parent: 置き先。
            row: グリッドの行番号。
            text: 左側に出す見出し。
            variable: 選択中のラベルを保持する変数。

        Returns:
            出来上がったドロップダウン。
        """
        ctk.CTkLabel(
            parent,
            text=text,
            font=self._font_eyebrow,
            text_color=theme.MUTED,
            width=120,
            anchor="w",
        ).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=(0, 8))

        menu = ctk.CTkOptionMenu(
            parent,
            variable=variable,
            values=[_NO_DEVICE],
            font=self._font_label,
            dynamic_resizing=False,
            corner_radius=10,
            height=34,
            fg_color=theme.PANEL,
            button_color=theme.over(theme.CYAN, theme.PANEL, 0.35),
            button_hover_color=theme.over(theme.CYAN, theme.PANEL, 0.55),
            text_color=theme.TEXT_DIM,
            dropdown_fg_color=theme.PANEL_SOFT,
            dropdown_hover_color=theme.over(theme.CYAN, theme.PANEL_SOFT, 0.25),
            dropdown_text_color=theme.TEXT_DIM,
            dropdown_font=self._font_label,
        )
        menu.grid(row=row, column=1, sticky="ew", pady=(0, 8))
        return menu

    def _build_key_panel(self) -> None:
        """キー変更の操作パネルを作る。"""
        panel = ctk.CTkFrame(
            self, fg_color=theme.PANEL, corner_radius=12, border_width=0
        )
        panel.grid(row=3, column=0, sticky="ew", padx=20, pady=(6, 0))
        panel.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            panel,
            text="キー",
            font=self._font_eyebrow,
            text_color=theme.MUTED,
            anchor="w",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(10, 0))

        self._down_button = ctk.CTkButton(
            panel,
            text="▼",
            font=self._font_title,
            width=56,
            height=52,
            command=lambda: self._nudge_key(-1),
        )
        self._down_button.grid(row=1, column=0, padx=(14, 0), pady=(2, 6))
        theme.style_ghost_button(self._down_button, theme.CYAN)

        self._key_label = ctk.CTkLabel(
            panel,
            text="±0",
            font=self._font_key,
            text_color=theme.CYAN,
        )
        self._key_label.grid(row=1, column=1, pady=(2, 0))

        self._up_button = ctk.CTkButton(
            panel,
            text="▲",
            font=self._font_title,
            width=56,
            height=52,
            command=lambda: self._nudge_key(1),
        )
        self._up_button.grid(row=1, column=2, padx=(0, 14), pady=(2, 6))
        theme.style_ghost_button(self._up_button, theme.CYAN)

        self._key_hint = ctk.CTkLabel(
            panel,
            text=f"原曲キー（{KEY_SHIFT_MIN}〜+{KEY_SHIFT_MAX} 半音）",
            font=self._font_small,
            text_color=theme.MUTED,
        )
        self._key_hint.grid(row=2, column=0, columnspan=3, pady=(0, 4))

        self._reset_button = ctk.CTkButton(
            panel,
            text="原曲キー",
            font=self._font_small,
            width=110,
            height=26,
            command=lambda: self._set_key(0),
        )
        self._reset_button.grid(row=3, column=0, columnspan=3, pady=(0, 12))
        theme.style_ghost_button(self._reset_button, theme.MUTED)

        # ▲▼ ボタンを押しに行かなくても操作できるようにする
        self.bind("<Up>", lambda _event: self._nudge_key(1))
        self.bind("<Down>", lambda _event: self._nudge_key(-1))

    def _build_control_row(self) -> None:
        """開始／停止ボタンと状態表示を作る。"""
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=4, column=0, sticky="ew", padx=20, pady=(12, 16))
        row.grid_columnconfigure(0, weight=1)

        self._toggle_button = ctk.CTkButton(
            row,
            text="▶ 開始",
            font=self._font_title,
            height=44,
            command=self._on_toggle,
        )
        self._toggle_button.grid(row=0, column=0, sticky="ew")
        theme.style_solid_button(self._toggle_button, theme.CYAN, theme.CYAN_HOVER)

        self._state_label = ctk.CTkLabel(
            row,
            text="停止中",
            font=self._font_small,
            text_color=theme.MUTED,
            anchor="w",
            justify="left",
            wraplength=560,
        )
        self._state_label.grid(row=1, column=0, sticky="w", pady=(8, 0))

    # ------------------------------------------------------------------
    #  デバイス一覧
    # ------------------------------------------------------------------
    def refresh_devices(self) -> None:
        """デバイス一覧を取り直し、それらしいものを選んでおく。

        動作中は入力・出力を変えられないので、一覧の更新もしない
        （選び直しても反映されず、かえって混乱するため）。
        """
        if self.monitor.is_running:
            return

        self._input_devices = list_input_devices()
        self._output_devices = list_output_devices()

        self._fill_menu(
            self._input_menu,
            self._input_var,
            self._input_devices,
            suggest_input_device(self._input_devices),
        )
        self._fill_menu(
            self._output_menu,
            self._output_var,
            self._output_devices,
            suggest_output_device(self._output_devices),
        )

    def _fill_menu(
        self,
        menu: ctk.CTkOptionMenu,
        variable: tk.StringVar,
        devices: list[dict[str, Any]],
        preferred: int | None,
    ) -> None:
        """ドロップダウンの中身を入れ替える。

        Args:
            menu: 対象のドロップダウン。
            variable: 選択中のラベルを保持する変数。
            devices: 並べるデバイス。
            preferred: 最初から選んでおくデバイス番号。None なら先頭。
        """
        labels = [str(device["label"]) for device in devices]
        if not labels:
            menu.configure(values=[_NO_DEVICE])
            variable.set(_NO_DEVICE)
            return

        menu.configure(values=labels)
        chosen = labels[0]
        if preferred is not None:
            for device, label in zip(devices, labels):
                if int(device["index"]) == preferred:
                    chosen = label
                    break
        variable.set(chosen)

    def _selected_index(
        self, variable: tk.StringVar, devices: list[dict[str, Any]]
    ) -> int | None:
        """選択中のラベルからデバイス番号を引く。

        Args:
            variable: 選択中のラベルを保持する変数。
            devices: 候補。

        Returns:
            デバイス番号。選ばれていなければ None。
        """
        label = variable.get()
        for device in devices:
            if str(device["label"]) == label:
                return int(device["index"])
        return None

    # ------------------------------------------------------------------
    #  操作
    # ------------------------------------------------------------------
    def _nudge_key(self, delta: int) -> None:
        """キーを ``delta`` 半音動かす。

        Args:
            delta: 動かす半音数（+1 / -1）。
        """
        self._set_key(self.monitor.key_shift + delta)

    def _set_key(self, semitones: int) -> None:
        """キーを設定して表示を更新する。

        Args:
            semitones: 半音数。範囲外は :class:`LiveMonitor` 側で丸められる。
        """
        self.monitor.set_key_shift(semitones)
        shift = self.monitor.key_shift
        self._key_label.configure(
            text="±0" if shift == 0 else f"{shift:+d}",
            text_color=theme.CYAN if shift == 0 else theme.VIOLET,
        )


    def _on_toggle(self) -> None:
        """開始／停止ボタンの処理。"""
        if self.monitor.is_running:
            self.monitor.stop()
            self._restore_default_output()
            # 既定デバイスの切り替え（COM呼び出し）でウィンドウが
            # 背面へ回ることがあるため、前面へ出し直す
            # （__init__ の同じ処理を参照）。
            theme.bring_to_front(self)
            self._update_state_label()
            return

        input_index = self._selected_index(self._input_var, self._input_devices)
        output_index = self._selected_index(self._output_var, self._output_devices)
        if input_index is None or output_index is None:
            messagebox.showwarning(
                "デバイスが選ばれていません",
                "入力と出力の両方を選んでから開始してください。\n"
                "一覧が空の場合は「一覧を更新」を押してください。",
                parent=self,
            )
            return

        self.monitor.input_device = input_index
        self.monitor.output_device = output_index
        try:
            self.monitor.start()
        except LiveMonitorError as exc:
            messagebox.showerror(
                with_code(exc, "キー変更を開始できません"), str(exc), parent=self
            )
        else:
            # 一度でも開始（＝接続成功）まで進んだら「もう手順は分かって
            # いる人」とみなし、この場と次回起動以降で手順パネルを
            # 畳んでおく。
            if not self._settings.live_monitor_guide_seen:
                self._settings.live_monitor_guide_seen = True
                save_settings(self._settings)
            if not self._guide_collapsed:
                self._guide_collapsed = True
                self._apply_guide_collapsed()
        self._update_state_label()

    def _on_close(self) -> None:
        """ウィンドウを閉じる。動いていれば必ず止めてから閉じる。"""
        if self._poll_id is not None:
            try:
                self.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        self.monitor.stop()
        self._restore_default_output()
        self.destroy()

    def _auto_switch_to_cable(self) -> bool:
        """既定の出力を CABLE Input へ自動で切り替える。

        VB-CABLE のセットアップ手順②（「既定の出力を CABLE Input に
        する」）を、利用者が Windows のサウンド設定を開かずに済むよう
        代わりにやる。:meth:`__init__` の一番はじめ、
        :attr:`_original_output_name` を記録した**直後**に呼ぶこと
        （記録より先に切り替えると、元の値が分からなくなる）。

        Returns:
            切り替えられたら True。``comtypes``/Windows が無い環境や
            VB-CABLE 未導入で見つからない場合は False（このときは
            :meth:`_build_guide` が手動手順を案内する）。
        """
        if not default_device.is_available():
            return False
        if self._original_output_name and "CABLE" in self._original_output_name:
            return True  # 既に CABLE 系。切り替え済みとして扱う
        return default_device.set_default_output_by_name("CABLE Input")

    def _restore_default_output(self) -> None:
        """既定の出力が CABLE 系のままなら、実際のスピーカー等へ戻す。

        VB-CABLE のセットアップ手順②で「既定の出力を CABLE Input に
        する」を利用者に手動でやってもらっているため、キー変更を
        やめた後もそのままだと**システムの音がどこにも出なくなる**
        （CABLE Input で受け止めてくれる相手が居なくなるため）。
        これを利用者が毎回手動で戻さなくて済むようにする。

        戻し先は次の優先順で決める。

        1. この画面を開いた時点の既定出力（:attr:`_original_output_name`）
           ただし、それ自体が CABLE 系だった場合は使わない（開く前から
           CABLE のままだった＝どれが「本来の」出力か分からないため）
        2. 画面の「出力（鳴らす先）」欄で選んでいるデバイス
           （実在する現実のデバイスのはずなので、無音になるよりまし）

        Windows 以外の環境や ``comtypes`` 未導入では
        :func:`audio.default_device.is_available` が False を返すため、
        何もせず静かに終わる。
        """
        if not default_device.is_available():
            return

        # eConsole 役割だけでなく3役割すべてを見る。eConsole だけ既に
        # 元へ戻っていても、他の役割が CABLE Input に取り残されている
        # ことがあり（実機で確認済み: 「たまに音が出ない・アプリを
        # 開き直すと直る」不具合の原因だった）、1役割だけの判定だと
        # それを見逃して復元をスキップしてしまう。
        if not default_device.is_any_role_still("CABLE"):
            return  # 既に CABLE 以外（利用者が自分で戻した等）なら触らない

        if self._original_output_name and "CABLE" not in self._original_output_name:
            default_device.set_default_output_by_name(self._original_output_name)
            return

        output_index = self._selected_index(self._output_var, self._output_devices)
        if output_index is None:
            return
        device = next(
            (d for d in self._output_devices if d["index"] == output_index), None
        )
        if device is not None:
            default_device.set_default_output_by_name(device["name"])

    # ------------------------------------------------------------------
    #  状態表示
    # ------------------------------------------------------------------
    def _schedule_poll(self) -> None:
        """状態表示の更新を予約する。"""
        self._poll_id = self.after(_POLL_MS, self._poll)

    def _poll(self) -> None:
        """定期的に状態表示を更新する。"""
        self._update_state_label()
        self._schedule_poll()

    def _update_state_label(self) -> None:
        """稼働状態とボタンの見た目をそろえる。"""
        running = self.monitor.is_running
        self._toggle_button.configure(text="■ 停止" if running else "▶ 開始")
        if running:
            theme.style_solid_button(
                self._toggle_button, theme.DANGER, theme.DANGER_HOVER, theme.TEXT
            )
        else:
            theme.style_solid_button(self._toggle_button, theme.CYAN, theme.CYAN_HOVER)

        state = "normal" if not running else "disabled"
        self._input_menu.configure(state=state)
        self._output_menu.configure(state=state)
        self._refresh_button.configure(state=state)

        error = self.monitor.error
        if error:
            self._guide_status_label.configure(
                text="● デバイスの接続に失敗", text_color=theme.DANGER
            )
            self._state_label.configure(text=f"⚠ {error}", text_color=theme.DANGER)
            return
        if not running:
            self._guide_status_label.configure(text="", text_color=theme.MUTED)
            self._state_label.configure(
                text="停止中。手順②まで済ませてから開始してください。",
                text_color=theme.MUTED,
            )
            return

        self._guide_status_label.configure(
            text="● デバイス接続中", text_color=theme.GREEN
        )

        layout = "ステレオ" if self.monitor.channels >= 2 else "モノラル"
        health = self.monitor.health
        text = (
            f"● 稼働中  {self.monitor.sample_rate} Hz・{layout}・"
            f"遅延 約 {self.monitor.latency_ms:.0f} ms"
        )
        if health["resyncs"] or health["starved"] > 8:
            # 起動直後の数回は先読みを溜める分なので、そこは数えない
            text += f"（音飛び {health['starved'] + health['resyncs']} 回）"
        self._state_label.configure(text=text, text_color=theme.GREEN)
