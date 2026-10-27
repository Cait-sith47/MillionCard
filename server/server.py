"""
Million Card Game - Local WebSocket Lobby & Battle Relay Server
標準ライブラリ（asyncio, socket, json）のみで動作する軽量WebSocketサーバーです。
外部パッケージのインストール（pip install）は不要です。
"""

import asyncio
import base64
import hashlib
import json
import os
import random
import secrets
import string
import struct
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "9080"))
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

try:
    with (Path(__file__).parent / "character_catalog.json").open(encoding="utf-8") as catalog_file:
        CHARACTER_CATALOG = json.load(catalog_file)
except (OSError, json.JSONDecodeError) as exc:
    raise RuntimeError("キャラクターデータ一覧を読み込めません: server/character_catalog.json") from exc

ACTIVE_ACTOR_ACTIONS = {
    "roll", "reroll", "keep_roll", "target_pressed", "use_sports_drink",
    "use_replay_ticket", "ogami_reroll", "matsuri_reroll",
    "megumi_after_school_reroll", "birth_of_color_reroll", "emily_free_reroll",
    "subaru_reroll", "yuriko_ap_reroll", "subaru_perfect_game", "subaru_home_run",
    "use_escape", "escape_cancel", "escape_target", "escape_lane",
    "shiki_flip_toggle", "shiki_flip_confirm", "shiki_proust_reveal",
    "shiki_draw_reveal", "frederica_skill_decided",
}
SERVER_RESOLUTION_SAFE_CARDS = {"mirai", "shizuka", "tsubasa"}

# 部屋データ
# room_id -> { "host": (writer, info), "guest": (writer, info) }
rooms = {}
# writer -> (room_id, role)
client_rooms = {}

def side_for_role(role: str) -> str:
    return "left" if role == "host" else "right"

def sanitize_player_info(value: object, fallback_name: str) -> dict:
    info = dict(value) if isinstance(value, dict) else {}
    raw_name = info.get("name", "")
    name = "".join(char for char in str(raw_name) if char.isprintable()).strip()[:16]
    info["name"] = name or fallback_name
    return info

def build_battle_state(room: dict, first_side: str) -> dict | None:
    """配置が確定した6人分の初期状態をサーバー側に作成します。"""
    characters = {}
    for role in ("host", "guest"):
        side = side_for_role(role)
        lanes = room.get("setup_lineups", {}).get(role)
        if not isinstance(lanes, dict) or set(lanes) != {"left", "center", "right"}:
            return None
        for lane in ("left", "center", "right"):
            card_id = lanes.get(lane)
            card = CHARACTER_CATALOG.get(card_id) if isinstance(card_id, str) else None
            if not isinstance(card, dict) or type(card.get("hp")) is not int or card["hp"] <= 0:
                return None
            character_id = f"{card_id}_{side}"
            if character_id in characters:
                return None
            characters[character_id] = {
                "card_id": card_id,
                "name": str(card.get("name", card_id)),
                "side": side,
                "lane": lane,
                "hp": card["hp"],
                "max_hp": card["hp"],
                "dex": int(card.get("dex", 0)),
                "attribute": str(card.get("attribute", "")),
                "status": {},
            }
    return {
        "characters": characters,
        "first_side": first_side,
        "round": 1,
        "completed_actions": 0,
        "turn_sequence": 0,
        "last_result_id": 0,
        "resolution_safe": all(
            character["card_id"] in SERVER_RESOLUTION_SAFE_CARDS
            for character in characters.values()
        ) and all(not disk_id for lanes in room.get("setup_disks", {}).values() for disk_id in lanes.values()),
        "pending_attacks": {"host": None, "guest": None},
    }

def simple_damage_action(card_id: str, roll: int) -> dict | None:
    card = CHARACTER_CATALOG.get(card_id, {})
    action = card.get("dice_actions", {}).get(str(roll))
    if not isinstance(action, dict) or "effect" in action:
        return None
    if type(action.get("damage")) is not int or action["damage"] <= 0:
        return None
    if action.get("target") not in ("any", "front"):
        return None
    if any(key in action for key in ("heal", "bonus_if_target_dex_lower", "bonus_per_hand_item", "unconditional_damage")):
        return None
    return action

def resolve_server_attack(room: dict, role: str, actor_id: str, target_id: str, turn_sequence: int) -> dict | None:
    state = room.get("battle_state")
    if not isinstance(state, dict) or not state.get("resolution_safe", False):
        return None
    pending = state.get("pending_attacks", {}).get(role)
    characters = state.get("characters", {})
    if not isinstance(pending, dict) or pending.get("actor_id") != actor_id or pending.get("turn_sequence") != turn_sequence:
        return None
    actor = characters.get(actor_id)
    target = characters.get(target_id)
    if not isinstance(actor, dict) or not isinstance(target, dict) or actor["side"] == target["side"]:
        return None
    if actor["hp"] <= 0 or target["hp"] <= 0:
        return None
    action = simple_damage_action(actor["card_id"], pending["roll"])
    if action is None:
        return None
    if action.get("target") == "front" and actor["lane"] != target["lane"]:
        return None

    attribute_edges = {"Vo": "Da", "Da": "Vi", "Vi": "Vo"}
    attacker_attribute = actor["attribute"]
    defender_attribute = target["attribute"]
    attribute_modifier = 200 if attribute_edges.get(attacker_attribute) == defender_attribute else (
        -200 if attribute_edges.get(defender_attribute) == attacker_attribute else 0
    )
    base_damage = action["damage"]
    # 通しのturn_sequenceは再戦後も続くため、ラウンド判定には使いません。
    # 安全判定中は1ラウンドに6人が各1回行動するため、完了行動数で数えます。
    completed_actions = int(state.get("completed_actions", 0))
    round_number = completed_actions // 6 + 1
    sudden_death_damage = 10000 if round_number > 10 else 0
    damage = max(0, base_damage + attribute_modifier) + sudden_death_damage
    target["hp"] = max(0, target["hp"] - damage)
    state["last_result_id"] += 1
    state["completed_actions"] = completed_actions + 1
    state["turn_sequence"] = turn_sequence
    state["round"] = round_number
    state["pending_attacks"][role] = None
    if target["hp"] == 0:
        # 倒した後は行動順や残存効果の判定が絡むため、次の攻撃から旧方式へ戻します。
        state["resolution_safe"] = False
    return {
        "type": "server_authoritative_result",
        "result_id": state["last_result_id"],
        "actor_id": actor_id,
        "target_id": target_id,
        "roll": pending["roll"],
        "action_name": str(action.get("name", "攻撃")),
        "base_damage": base_damage,
        "attribute_modifier": attribute_modifier,
        "sudden_death_damage": sudden_death_damage,
        "damage": damage,
        "target_hp": target["hp"],
        "target_max_hp": target["max_hp"],
        "resolution_safe": state["resolution_safe"],
    }

async def reject_room_battle(room: dict, offender_role: str, reason: str):
    """両方の画面へ中断を通知し、この部屋の対戦操作を停止します。"""
    room["protocol_failed"] = True
    offender_side = side_for_role(offender_role)
    abort_data = {"type": "sync_abort", "reason": reason}
    for recipient_role in ("host", "guest"):
        participant = room.get(recipient_role)
        if participant and participant[0]:
            await send_msg(participant[0], {
                "type": "relay",
                "sender_side": offender_side,
                "data": abort_data,
            })

def valid_battle_action(data, expected_sequence: int) -> bool:
    if not isinstance(data, dict):
        return False
    action_name = data.get("action")
    action_data = data.get("data", {})
    sequence = data.get("sequence")
    checksum = data.get("state_checksum")
    if not isinstance(action_name, str) or not action_name or len(action_name) > 64:
        return False
    if not isinstance(action_data, dict):
        return False
    # bool は Python では int の一種なので、型を完全一致で確認します。
    if type(sequence) is not int or sequence != expected_sequence:
        return False
    if not isinstance(checksum, str) or len(checksum) != 64:
        return False
    return all(char in "0123456789abcdefABCDEF" for char in checksum)

def make_handshake_response(key: str) -> bytes:
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    )
    return resp.encode()

def encode_ws_frame(text: str) -> bytes:
    payload = text.encode("utf-8")
    length = len(payload)
    if length <= 125:
        header = struct.pack("!BB", 0x81, length)
    elif length <= 65535:
        header = struct.pack("!BBH", 0x81, 126, length)
    else:
        header = struct.pack("!BBQ", 0x81, 127, length)
    return header + payload

async def send_msg(writer: asyncio.StreamWriter, data: dict):
    try:
        raw = encode_ws_frame(json.dumps(data, ensure_ascii=False))
        writer.write(raw)
        await writer.drain()
    except Exception as e:
        print(f"[ERROR] メッセージ送信失敗: {e}")

async def decode_ws_frame(reader: asyncio.StreamReader) -> str:
    try:
        head = await reader.read(2)
        if not head or len(head) < 2:
            return None
        b1, b2 = head
        opcode = b1 & 0x0F
        if opcode == 0x8:  # 接続切断フレーム
            return None
        masked = bool(b2 & 0x80)
        payload_len = b2 & 0x7F
        if payload_len == 126:
            data = await reader.read(2)
            if len(data) < 2:
                return None
            payload_len = struct.unpack("!H", data)[0]
        elif payload_len == 127:
            data = await reader.read(8)
            if len(data) < 8:
                return None
            payload_len = struct.unpack("!Q", data)[0]

        mask = None
        if masked:
            mask = await reader.read(4)
            if len(mask) < 4:
                return None

        payload = await reader.read(payload_len)
        if len(payload) < payload_len:
            return None
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        return payload.decode("utf-8", errors="replace")
    except Exception:
        return None

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    peer_name = writer.get_extra_info("peername")
    
    # 最初のHTTPハンドシェイクリクエストを受信
    request_data = b""
    while b"\r\n\r\n" not in request_data:
        chunk = await reader.read(1024)
        if not chunk:
            writer.close()
            return
        request_data += chunk
        if len(request_data) > 8192:
            writer.close()
            return

    header_text = request_data.decode("utf-8", errors="replace")
    lines = header_text.split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    # WebSocket接続かどうか判定
    if "upgrade" in headers.get("connection", "").lower() and headers.get("upgrade", "").lower() == "websocket":
        key = headers.get("sec-websocket-key")
        if not key:
            writer.close()
            return
        writer.write(make_handshake_response(key))
        await writer.drain()
        print(f"[CONNECT] クライアントがWebSocket接続しました: {peer_name}")
    else:
        # ブラウザ等で通常アクセスされた場合の確認用HTML
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Million Card Server</title></head>"
            "<body style='font-family:sans-serif; text-align:center; padding:50px; background:#1a1a24; color:#fff;'>"
            "<h2>🎴 Million Card 対戦サーバー稼働中</h2>"
            f"<p>状態: 正常稼働 / 現在の対戦部屋数: <b>{len(rooms)}</b></p>"
            "<p style='color:#aaa;'>Godotクライアントから ws://127.0.0.1:9080 に接続してください。</p>"
            "</body></html>"
        ).encode("utf-8")
        resp = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"Access-Control-Allow-Origin: *\r\n"
            b"Content-Length: " + str(len(html)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + html
        )
        writer.write(resp)
        await writer.drain()
        writer.close()
        return

    # WebSocketメッセージ受信ループ
    try:
        while True:
            text = await decode_ws_frame(reader)
            if text is None:
                break
            try:
                msg = json.loads(text)
            except Exception:
                continue

            mtype = msg.get("type", "")

            # 部屋の作成
            if mtype == "create_room":
                # 4桁の英数字部屋コードを生成
                room_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
                while room_id in rooms:
                    room_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))

                player_info = sanitize_player_info(msg.get("player_info", {}), "ホスト")
                rooms[room_id] = {
                    "host": (writer, player_info),
                    "guest": None,
                    "battle_started": False,
                    "battle_action_sequences": {"host": 0, "guest": 0},
                    "rematch_requests": set(),
                    "protocol_failed": False,
                    "random_results": {"host": {}, "guest": {}},
                    "pending_server_rolls": {"host": None, "guest": None},
                    "setup_bans": {"host": "", "guest": ""},
                    "setup_lineups": {"host": None, "guest": None},
                    "setup_disks": {"host": None, "guest": None},
                    "battle_character_ids": {"host": [], "guest": []},
                    "battle_state": None,
                    "last_action_turn": None,
                    "last_action_actor": "",
                }
                client_rooms[writer] = (room_id, "host")
                print(f"[ROOM] 部屋が作成されました: コード「{room_id}」（ホスト: {player_info.get('name', 'P1')}）")
                await send_msg(writer, {
                    "type": "room_created",
                    "room_id": room_id,
                    "player_side": "left",
                    "message": f"部屋を作成しました。部屋コード: {room_id}"
                })

            # 部屋への参加
            elif mtype == "join_room":
                target_room = str(msg.get("room_id", "")).strip().upper()
                if target_room not in rooms:
                    await send_msg(writer, {
                        "type": "error",
                        "message": f"部屋「{target_room}」が見つかりませんでした。"
                    })
                    continue

                room = rooms[target_room]
                if room["guest"] is not None:
                    await send_msg(writer, {
                        "type": "error",
                        "message": "この部屋はすでに満員（2名参加中）です。"
                    })
                    continue

                guest_info = sanitize_player_info(msg.get("player_info", {}), "ゲスト")
                room["guest"] = (writer, guest_info)
                client_rooms[writer] = (target_room, "guest")
                print(f"[ROOM] ゲストが参加しました: 部屋「{target_room}」（ゲスト: {guest_info.get('name', 'P2')}）")

                host_info = room["host"][1]
                # ゲストへ入室完了を通知
                await send_msg(writer, {
                    "type": "room_joined",
                    "room_id": target_room,
                    "player_side": "right",
                    "peer_info": host_info,
                    "message": f"部屋「{target_room}」に参加しました！"
                })
                # ホストへ相手の参加を通知
                host_writer = room["host"][0]
                await send_msg(host_writer, {
                    "type": "peer_joined",
                    "room_id": target_room,
                    "peer_info": guest_info,
                    "message": "対戦相手が部屋に参加しました！"
                })

            # 対戦中のサイコロ結果はサーバー側で生成し、要求した本人へ返します。
            elif mtype == "random_request":
                if writer not in client_rooms:
                    await send_msg(writer, {"type": "error", "message": "対戦部屋に参加していません。"})
                    continue
                r_id, role = client_rooms[writer]
                room = rooms.get(r_id)
                request_id = msg.get("request_id")
                count = msg.get("count")
                if not room or room.get("protocol_failed", False) or not room.get("battle_started", False):
                    await send_msg(writer, {"type": "error", "message": "対戦中の乱数要求ではありません。"})
                    continue
                if not isinstance(request_id, str) or not request_id or len(request_id) > 96 or type(count) is not int or count not in (1, 2):
                    await reject_room_battle(room, role, "乱数要求の形式を確認できなかったため、対戦を終了します。")
                    continue
                role_cache = room["random_results"].setdefault(role, {})
                if request_id in role_cache:
                    rolls = role_cache[request_id]
                    if len(rolls) != count:
                        await reject_room_battle(room, role, "同じ乱数要求が異なる内容で届いたため、対戦を終了します。")
                        continue
                else:
                    rolls = [secrets.randbelow(6) + 1 for _ in range(count)]
                    role_cache[request_id] = rolls
                    while len(role_cache) > 64:
                        role_cache.pop(next(iter(role_cache)))
                room["pending_server_rolls"][role] = rolls
                await send_msg(writer, {
                    "type": "random_result",
                    "request_id": request_id,
                    "rolls": rolls,
                })

            # 対戦中のメッセージ中継
            elif mtype == "relay":
                if writer in client_rooms:
                    r_id, role = client_rooms[writer]
                    room = rooms.get(r_id)
                    if room:
                        if room.get("protocol_failed", False):
                            continue
                        data = msg.get("data", {})
                        if not isinstance(data, dict):
                            await reject_room_battle(room, role, "通信データの形式を確認できなかったため、対戦を終了します。")
                            continue
                        data_type = str(data.get("type", ""))
                        if data_type == "action":
                            expected_sequence = int(room["battle_action_sequences"].get(role, 0)) + 1
                            if not room.get("battle_started", False) or not valid_battle_action(data, expected_sequence):
                                await reject_room_battle(room, role, "対戦操作の順番または内容を確認できなかったため、対戦を終了します。")
                                continue
                            actor_id = data.get("actor_id")
                            turn_sequence = data.get("turn_sequence")
                            known_actor_ids = room["battle_character_ids"].get("host", []) + room["battle_character_ids"].get("guest", [])
                            if type(turn_sequence) is not int or turn_sequence < 0 or not isinstance(actor_id, str):
                                await reject_room_battle(room, role, "操作キャラクターの情報を確認できませんでした。")
                                continue
                            if actor_id:
                                previous_turn = room.get("last_action_turn")
                                if actor_id not in known_actor_ids or (previous_turn is not None and turn_sequence < previous_turn):
                                    await reject_room_battle(room, role, "操作キャラクターまたは行動順が正しくありません。")
                                    continue
                                if data.get("action") in ACTIVE_ACTOR_ACTIONS and not actor_id.endswith("_" + side_for_role(role)):
                                    await reject_room_battle(room, role, "現在の行動キャラクターを操作できる側ではありません。")
                                    continue
                                if previous_turn == turn_sequence and room.get("last_action_actor") != actor_id:
                                    await reject_room_battle(room, role, "同じ行動順の途中で操作キャラクターが変わりました。")
                                    continue
                                room["last_action_turn"] = turn_sequence
                                room["last_action_actor"] = actor_id
                            elif data.get("action") not in ("use_round_start_item", "skip_round_item"):
                                await reject_room_battle(room, role, "操作キャラクターが指定されていません。")
                                continue
                            action_data = data.get("data", {})
                            for target_key in ("target_id", "opp_id", "ally_id", "source_id", "dest_id"):
                                target_id = action_data.get(target_key)
                                if target_id is not None and (not isinstance(target_id, str) or target_id not in known_actor_ids):
                                    await reject_room_battle(room, role, "対象キャラクターが対戦に参加していません。")
                                    break
                            if room.get("protocol_failed", False):
                                continue
                            if data.get("action") in ("escape_target", "escape_lane"):
                                escape_data = action_data
                                lane_snapshot = escape_data.get("side_lanes")
                                expected_ids = room["battle_character_ids"].get(role, [])
                                state = room.get("battle_state")
                                state_characters = state.get("characters") if isinstance(state, dict) else None
                                if (
                                    not isinstance(lane_snapshot, dict)
                                    or set(lane_snapshot) != set(expected_ids)
                                    or not isinstance(state_characters, dict)
                                    or any(lane not in ("left", "center", "right") for lane in lane_snapshot.values())
                                ):
                                    await reject_room_battle(room, role, "エスケープの配置情報を確認できませんでした。")
                                    continue
                                if data.get("action") == "escape_target":
                                    target_id = escape_data.get("target_id")
                                    if (
                                        not isinstance(target_id, str)
                                        or target_id == actor_id
                                        or target_id not in expected_ids
                                        or state_characters.get(target_id, {}).get("hp", 0) <= 0
                                    ):
                                        await reject_room_battle(room, role, "エスケープの入れ替え対象を確認できませんでした。")
                                        continue
                                elif escape_data.get("empty_lane") not in ("left", "center", "right"):
                                    await reject_room_battle(room, role, "エスケープの移動先を確認できませんでした。")
                                    continue
                                for character_id, lane in lane_snapshot.items():
                                    state_characters[character_id]["lane"] = lane
                            if data.get("action") == "roll":
                                submitted_rolls = data.get("data", {}).get("rolls")
                                expected_rolls = room["pending_server_rolls"].get(role)
                                if not isinstance(submitted_rolls, list) or submitted_rolls != expected_rolls:
                                    await reject_room_battle(room, role, "サーバーが決めたサイコロ結果と一致しないため、対戦を終了します。")
                                    continue
                                room["pending_server_rolls"][role] = None
                                state = room.get("battle_state")
                                if isinstance(state, dict) and state.get("resolution_safe", False):
                                    action = simple_damage_action(actor_id.rsplit("_", 1)[0], submitted_rolls[0]) if len(submitted_rolls) == 1 else None
                                    if action is None:
                                        state["resolution_safe"] = False
                                        state["pending_attacks"][role] = None
                                    else:
                                        state["pending_attacks"][role] = {
                                            "actor_id": actor_id,
                                            "turn_sequence": turn_sequence,
                                            "roll": submitted_rolls[0],
                                        }
                            state = room.get("battle_state")
                            if isinstance(state, dict) and data.get("action") == "target_pressed":
                                result = resolve_server_attack(
                                    room,
                                    role,
                                    actor_id,
                                    str(action_data.get("target_id", "")),
                                    turn_sequence,
                                )
                                if result is not None:
                                    room["battle_action_sequences"][role] = expected_sequence
                                    other_role = "guest" if role == "host" else "host"
                                    other = room.get(other_role)
                                    if other and other[0]:
                                        await send_msg(other[0], {
                                            "type": "relay",
                                            "sender_side": side_for_role(role),
                                            "data": {
                                                "type": "action",
                                                "action": data.get("action", ""),
                                                "data": action_data,
                                                "actor_id": actor_id,
                                                "turn_sequence": turn_sequence,
                                                "sequence": expected_sequence,
                                                "state_checksum": data.get("state_checksum", ""),
                                            },
                                        })
                                    for recipient_role in ("host", "guest"):
                                        participant = room.get(recipient_role)
                                        if participant and participant[0]:
                                            await send_msg(participant[0], {
                                                "type": "relay",
                                                "sender_side": "server",
                                                "data": result,
                                            })
                                    continue
                                if state.get("resolution_safe", False):
                                    await reject_room_battle(room, role, "サーバーが技の対象または結果を確認できませんでした。")
                                    continue
                            elif isinstance(state, dict) and data.get("action") not in ("roll", "skip_round_item"):
                                state["resolution_safe"] = False
                            room["battle_action_sequences"][role] = expected_sequence
                        elif data_type == "net_ban_confirmed":
                            ban_id = data.get("ban_id")
                            if room.get("battle_started", False) or room["setup_bans"].get(role) or not isinstance(ban_id, str) or not ban_id or len(ban_id) > 128:
                                await reject_room_battle(room, role, "BAN操作の段階または内容を確認できませんでした。")
                                continue
                            room["setup_bans"][role] = ban_id
                        elif data_type == "net_lineup_confirmed":
                            lanes = data.get("lanes")
                            disks = data.get("disks")
                            if room.get("battle_started", False) or room["setup_lineups"].get(role) is not None or not all(room["setup_bans"].values()):
                                await reject_room_battle(room, role, "配置操作の段階を確認できませんでした。")
                                continue
                            if not isinstance(lanes, dict) or set(lanes.keys()) != {"left", "center", "right"}:
                                await reject_room_battle(room, role, "配置操作の形式を確認できませんでした。")
                                continue
                            lane_ids = [lanes.get("left"), lanes.get("center"), lanes.get("right")]
                            if any(not isinstance(card_id, str) or not card_id or len(card_id) > 128 or card_id not in CHARACTER_CATALOG for card_id in lane_ids) or len(set(lane_ids)) != 3:
                                await reject_room_battle(room, role, "配置するカードを確認できませんでした。")
                                continue
                            if not isinstance(disks, dict) or set(disks.keys()) != {"left", "center", "right"} or any(not isinstance(disks.get(lane), str) or len(disks[lane]) > 128 for lane in ("left", "center", "right")):
                                await reject_room_battle(room, role, "配置したカードのディスク情報を確認できませんでした。")
                                continue
                            opponent_role = "guest" if role == "host" else "host"
                            if room["setup_bans"].get(opponent_role) in lane_ids:
                                await reject_room_battle(room, role, "相手がBANしたカードが配置に含まれています。")
                                continue
                            room["setup_lineups"][role] = dict(lanes)
                            room["setup_disks"][role] = dict(disks)
                            room["battle_character_ids"][role] = [f"{card_id}_{side_for_role(role)}" for card_id in lane_ids]
                        elif data_type == "net_battle_start_request":
                            if role != "host" or room.get("battle_started", False) or not all(room["setup_lineups"].values()):
                                await reject_room_battle(room, role, "対戦開始の通信を確認できなかったため、対戦を終了します。")
                                continue
                            first_side = secrets.choice(("left", "right"))
                            battle_state = build_battle_state(room, first_side)
                            if battle_state is None:
                                await reject_room_battle(room, role, "サーバーで対戦キャラクターの初期状態を作成できませんでした。")
                                continue
                            start_data = {
                                "type": "net_battle_start",
                                "first_side": first_side,
                                "rng_seed": secrets.randbits(32),
                                "server_resolution_safe": bool(battle_state.get("resolution_safe", False)),
                            }
                            room["battle_action_sequences"] = {"host": 0, "guest": 0}
                            room["pending_server_rolls"] = {"host": None, "guest": None}
                            room["battle_state"] = battle_state
                            room["battle_started"] = True
                            for recipient_role in ("host", "guest"):
                                participant = room.get(recipient_role)
                                if participant and participant[0]:
                                    await send_msg(participant[0], {
                                        "type": "relay",
                                        "sender_side": "server",
                                        "data": start_data,
                                    })
                        elif data_type == "rematch_request":
                            room["rematch_requests"].add(role)
                            if len(room["rematch_requests"]) == 2:
                                room["battle_started"] = False
                                room["battle_action_sequences"] = {"host": 0, "guest": 0}
                                room["pending_server_rolls"] = {"host": None, "guest": None}
                                room["setup_bans"] = {"host": "", "guest": ""}
                                room["setup_lineups"] = {"host": None, "guest": None}
                                room["setup_disks"] = {"host": None, "guest": None}
                                room["battle_character_ids"] = {"host": [], "guest": []}
                                room["battle_state"] = None
                                room["last_action_turn"] = None
                                room["last_action_actor"] = ""
                                room["rematch_requests"].clear()
                        other = room["guest"] if role == "host" else room["host"]
                        if data_type != "net_battle_start_request" and other and other[0]:
                            await send_msg(other[0], {
                                "type": "relay",
								"sender_side": side_for_role(role),
								"data": data
                            })

    finally:
        # 切断処理
        if writer in client_rooms:
            r_id, role = client_rooms[writer]
            del client_rooms[writer]
            if r_id in rooms:
                room = rooms[r_id]
                other = room["guest"] if role == "host" else room["host"]
                if other and other[0]:
                    await send_msg(other[0], {
                        "type": "peer_left",
                        "message": "対戦相手が切断しました。"
                    })
                del rooms[r_id]
                print(f"[ROOM] 部屋が解散されました: {r_id}")
        writer.close()
        await writer.wait_closed()
        print(f"[DISCONNECT] クライアントが切断しました: {peer_name}")

async def main():
    server = await asyncio.start_server(handle_client, HOST, PORT)
    print("=" * 60)
    print(f"[*] Million Card WebSocket Lobby Server 起動完了")
    print(f"・アドレス: ws://127.0.0.1:{PORT}")
    print(f"・ブラウザ確認用: http://127.0.0.1:{PORT}")
    print("・終了するには Ctrl + C を押してください")
    print("=" * 60)
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nサーバーを停止しました。")
