import json
import ssl
import time
import threading
import datetime
import attr
import paho.mqtt.client as mqtt
from urllib.parse import urlparse
from _core._utils import generate_session_id, generate_client_id, json_minimal
from _features._thread import *

class listeningEvent:
    _on_message = attr.ib()
    def __init__(self, dataFB):
        self.bodyResults = {
            "body": None,
            "timestamp": 0,
            "userID": 0,
            "messageID": None,
            "replyToID": 0,
            "type": None,
            "attachments": {"id": 0, "url": None},
        }
        self.syncToken   = None
        self.lastSeqID   = None
        self.dataFB      = dataFB
        self.retry_count = 0
        self.max_retries = 3
        self._reconnect_lock = threading.Lock()
        self._reconnecting   = False
        try:
            self.fbt = _all_thread_data.func(dataFB)
        except Exception as err:
            print(f"[{datetime.datetime.now()}] failed initial thread data fetch: {err}")
            self.fbt = {}

    # ------------------------------------------------------------------ #
    # seq_id helpers                                                       #
    # ------------------------------------------------------------------ #
    def _coerce_seq_id(self, value, source="seq_id"):
        try:
            seq_id = int(str(value).strip())
        except (TypeError, ValueError):
            print(f"[{datetime.datetime.now()}] ignore invalid {source}: {value}")
            return None
        if seq_id < 0:
            print(f"[{datetime.datetime.now()}] ignore negative {source}: {seq_id}")
            return None
        return seq_id

    def _set_last_seq_id(self, value, source="seq_id", allow_reset=False):
        seq_id = self._coerce_seq_id(value, source)
        if seq_id is None:
            return False
        previous = self._coerce_seq_id(self.lastSeqID) if self.lastSeqID is not None else None
        if previous is not None and seq_id < previous and not allow_reset:
            print(f"[{datetime.datetime.now()}] ignore stale {source}: {seq_id} < {previous}")
            return False
        self.lastSeqID = seq_id
        return True

    def get_last_seq_id(self):
        previousSeqID = self.lastSeqID
        try:
            self.fbt  = _all_thread_data.func(self.dataFB)
            nextSeqID = self.fbt.get("last_seq_id")
            if not self._set_last_seq_id(nextSeqID, "graphql sync_sequence_id", allow_reset=True):
                if previousSeqID is not None:
                    self.lastSeqID = previousSeqID
        except Exception as err:
            print(f"[{datetime.datetime.now()}] failed refreshing last_seq_id: {err}")
            if previousSeqID is not None:
                self.lastSeqID = previousSeqID
        print(f"[{datetime.datetime.now()}] last_seq_id: {self.lastSeqID}")

    # ------------------------------------------------------------------ #
    # safe reconnect — chạy trong thread riêng, KHÔNG gọi từ callback    #
    # ------------------------------------------------------------------ #
    def _schedule_reconnect(self, delay=10):
        """Đặt lịch reconnect trong thread riêng để tránh đệ quy."""
        if self._reconnecting:
            return
        def _do():
            with self._reconnect_lock:
                if self._reconnecting:
                    return
                self._reconnecting = True
            try:
                print(f"[{datetime.datetime.now()}] Reconnecting in {delay}s...")
                time.sleep(delay)
                self.syncToken   = None
                self.retry_count = 0
                self.connect_mqtt()
            except Exception as e:
                print(f"[{datetime.datetime.now()}] Reconnect error: {e}")
            finally:
                self._reconnecting = False
        threading.Thread(target=_do, daemon=True).start()

    # ------------------------------------------------------------------ #
    # connect                                                              #
    # ------------------------------------------------------------------ #
    def connect_mqtt(self):
        self._reconnecting = False   # reset flag khi bắt đầu kết nối mới
        self.retry_count   = 0

        session_id = generate_session_id()
        user = {
            "u":           self.dataFB["FacebookID"],
            "s":           session_id,
            "chat_on":     json_minimal(True),
            "fg":          False,
            "d":           generate_client_id(),
            "ct":          "websocket",
            "aid":         219994525426954,
            "mqtt_sid":    "",
            "cp":          3,
            "ecp":         10,
            "st":          "/t_ms",
            "pm":          [],
            "dc":          "",
            "no_auto_fg":  True,
            "gas":         None,
            "pack":        [],
        }

        host    = f"wss://edge-chat.facebook.com/chat?region=eag&sid={session_id}"
        options = {
            "client_id": "mqttwsclient",
            "username":  json_minimal(user),
            "clean":     True,
            "ws_options": {
                "headers": {
                    "Cookie":     self.dataFB["cookieFacebook"],
                    "Origin":     "https://www.facebook.com",
                    "User-Agent": "Mozilla/5.0 (Linux; Android 9; SM-G973U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/69.0.3497.100 Mobile Safari/537.36",
                    "Referer":    "https://www.facebook.com/",
                    "Host":       "edge-chat.facebook.com",
                },
            },
            "keepalive": 10,
        }

        # ---- callbacks ------------------------------------------------ #
        def _on_connect(client, userdata, flags, rc):
            if self.syncToken is None:
                self.get_last_seq_id()
            elif self.lastSeqID is None:
                print("syncToken exists but last_seq_id missing; recreating queue.")
                self.syncToken = None
                self.get_last_seq_id()

            if self.lastSeqID is None:
                print("ERR last_seq_id is None. Refresh cookie and restart.")
                client.disconnect()
                return

            topics = None
            queue  = {
                "sync_api_version":          10,
                "max_deltas_able_to_process": 1000,
                "delta_batch_size":          500,
                "encoding":                  "JSON",
                "entity_fbid":               self.dataFB["FacebookID"],
                "orca_version":              "1.2.0",
            }
            if self.syncToken is None:
                topics             = "/messenger_sync_create_queue"
                queue["initial_titan_sequence_id"] = self.lastSeqID
                queue["device_params"]             = None
            else:
                topics                = "/messenger_sync_get_diffs"
                queue["last_seq_id"]  = self.lastSeqID
                queue["sync_token"]   = self.syncToken

            print(f"Publishing to {topics} with seq_id: {self.lastSeqID}")
            client.publish(topics, json_minimal(queue), qos=1, retain=False)

        def _on_message(client, userdata, msg):
            try:
                j = json.loads(msg.payload.decode())

                # --- parse tin nhắn ---
                if j.get("deltas") is not None:
                    delta = j["deltas"][0]
                    meta  = delta.get("messageMetadata")
                    if meta:
                        thread_key = meta.get("threadKey", {})
                        self.bodyResults.update({
                            "body":      delta.get("body"),
                            "timestamp": meta.get("timestamp", 0),
                            "userID":    meta.get("actorFbId"),
                            "messageID": meta.get("messageId"),
                            "replyToID": thread_key.get("otherUserFbId") or thread_key.get("threadFbId"),
                            "type":      "user" if thread_key.get("otherUserFbId") else "thread",
                        })
                        # tên người gửi
                        self.bodyResults["senderName"] = delta.get("senderName") or meta.get("actorFbId","")
                        # attachments
                        atts = delta.get("attachments", [])
                        if atts:
                            try:
                                self.bodyResults["attachments"]["id"]  = atts[0]["fbid"]
                                self.bodyResults["attachments"]["url"] = atts[0]["mercury"]["blob_attachment"]["preview"]["uri"]
                            except (KeyError, TypeError, IndexError):
                                pass

                # --- syncToken ---
                if "syncToken" in j and "firstDeltaSeqId" in j:
                    self.syncToken   = j["syncToken"]
                    self._set_last_seq_id(
                        j.get("lastIssuedSeqId") or j.get("firstDeltaSeqId"),
                        "mqtt first/last seq_id"
                    )
                    self.retry_count = 0
                    return

                if "lastIssuedSeqId" in j:
                    self._set_last_seq_id(j["lastIssuedSeqId"], "mqtt lastIssuedSeqId")

                # --- errorCode ---
                if "errorCode" in j:
                    error = j["errorCode"]
                    print(f"ERR {error}")
                    is_overflow = error == 100 or str(error).upper() == "ERROR_QUEUE_OVERFLOW"

                    if is_overflow:
                        self.retry_count += 1
                        self.syncToken    = None
                        self.get_last_seq_id()

                        if self.lastSeqID is None:
                            print("Cannot recover: last_seq_id=None. Reconnecting...")
                            client.disconnect()
                            self._schedule_reconnect(10)   # ← thread riêng
                            return

                        if self.retry_count > self.max_retries:
                            print("Max retries — full reconnect")
                            client.disconnect()
                            self._schedule_reconnect(15)   # ← thread riêng
                            return

                        # thử re-create queue
                        queue = {
                            "sync_api_version":           10,
                            "max_deltas_able_to_process": 1000,
                            "delta_batch_size":           500,
                            "encoding":                   "JSON",
                            "entity_fbid":                self.dataFB["FacebookID"],
                            "initial_titan_sequence_id":  self.lastSeqID,
                            "device_params":              None,
                            "orca_version":               "1.2.0",
                        }
                        client.publish("/messenger_sync_create_queue", json_minimal(queue), qos=1, retain=False)
                    else:
                        # lỗi khác → reconnect qua thread
                        print(f"Unknown error {error} — reconnecting...")
                        client.disconnect()
                        self._schedule_reconnect(10)   # ← thread riêng

            except UnicodeDecodeError:
                print("ERR Failed parsing MQTT payload")
            except Exception as e:
                print(f"ERR on_message: {e}")

        def _on_disconnect(client, userdata, rc):
            print(f"[{datetime.datetime.now()}] Disconnected rc={rc}")
            if rc != 0:
                print("Unexpected disconnect — scheduling reconnect...")
                self._schedule_reconnect(10)   # ← thread riêng, KHÔNG gọi trực tiếp

        def _on_subscribe(client, userdata, mid, granted_qos):
            print(f"Subscribed: {mid} {granted_qos}")

        # ---- build client --------------------------------------------- #
        self.mqtt = mqtt.Client(
            client_id=options["client_id"],
            clean_session=options["clean"],
            protocol=mqtt.MQTTv31,
            transport="websockets",
        )
        self.mqtt.tls_set(
            certfile=None, keyfile=None,
            cert_reqs=ssl.CERT_NONE,
            tls_version=ssl.PROTOCOL_TLSv1_2,
        )
        self.mqtt.on_connect    = _on_connect
        self.mqtt.on_message    = _on_message
        self.mqtt.on_disconnect = _on_disconnect
        self.mqtt.on_subscribe  = _on_subscribe
        self.mqtt.username_pw_set(username=options["username"])

        parsed_host = urlparse(host)
        self.mqtt.ws_set_options(
            path=f"{parsed_host.path}?{parsed_host.query}",
            headers=options["ws_options"]["headers"],
        )
        self.mqtt.connect(
            host=options["ws_options"]["headers"]["Host"],
            port=443,
            keepalive=options["keepalive"],
        )
        self.mqtt.loop_forever()
