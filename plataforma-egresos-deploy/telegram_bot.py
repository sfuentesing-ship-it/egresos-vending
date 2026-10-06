"""Bot de Telegram para alertas y consultas de stock/egresos/ingresos."""
import json
import os
import threading
import time

import requests

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBot:
    def __init__(self, token, chats_path, command_handler, alert_checker):
        self.token = token
        self.chats_path = chats_path
        self.handler = command_handler      # fn(text, chat_id) -> str | None
        self.alert_checker = alert_checker  # fn() -> None (envía alertas si hay)
        self.offset = 0
        self.chats = self._load_chats()
        self._stop = False

    # ---------- persistencia de chats ----------
    def _load_chats(self):
        try:
            with open(self.chats_path, encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()

    def _save_chats(self):
        try:
            with open(self.chats_path, "w", encoding="utf-8") as f:
                json.dump(sorted(self.chats), f)
        except Exception:
            pass

    # ---------- API Telegram ----------
    def send(self, chat_id, text):
        url = API.format(token=self.token, method="sendMessage")
        for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
            try:
                requests.post(url, json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }, timeout=30)
            except Exception:
                pass

    def notify_all(self, text):
        for chat in list(self.chats):
            self.send(chat, text)

    # ---------- polling ----------
    def _poll(self):
        url = API.format(token=self.token, method="getUpdates")
        r = requests.get(url, params={"offset": self.offset, "timeout": 25}, timeout=40)
        data = r.json()
        for upd in data.get("result", []):
            self.offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = (msg.get("chat") or {}).get("id")
            text = (msg.get("text") or "").strip()
            if chat is None:
                continue
            if chat not in self.chats:
                self.chats.add(chat)
                self._save_chats()
            if not text:
                continue
            try:
                reply = self.handler(text, chat)
            except Exception as e:
                reply = "Error procesando el comando: " + str(e)[:200]
            if reply:
                self.send(chat, reply)

    def _run_polling(self):
        while not self._stop:
            try:
                self._poll()
            except Exception:
                time.sleep(5)

    def _run_alerts(self):
        # Revisa stock cada 4 horas y avisa cuando algo entra en estado crítico
        time.sleep(20)
        while not self._stop:
            try:
                self.alert_checker()
            except Exception:
                pass
            time.sleep(4 * 60 * 60)

    def start(self):
        threading.Thread(target=self._run_polling, daemon=True).start()
        threading.Thread(target=self._run_alerts, daemon=True).start()
