import requests
import time
import random
import threading
import queue
import logging
import os
from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit
from datetime import datetime

# Logging setup untuk WebSocket
log_queue = queue.Queue(-1)  # unlimited size
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')
logger = logging.getLogger()
class QueueHandler(logging.Handler):
    def emit(self, record):
        log_queue.put(self.format(record))
logger.addHandler(QueueHandler())

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super-secret-change-me')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'postgresql://postgres:aqzTxaTmylBvVRcwDrwslGHKJjpTmhYD@postgres.railway.internal:5432/railway')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

BASE_URL = os.environ.get('BASE_URL', "https://mort-royal-production.up.railway.app/api")
AGENT_NAMES = [f"regedx{i}" for i in range(1, 6)]

class ApiKey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slot = db.Column(db.Integer, unique=True, nullable=False)  # 1 sampai 5
    api_key = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# Thread broadcaster logs ke semua client
def broadcast_logs():
    while True:
        try:
            msg = log_queue.get(timeout=1)
            socketio.emit('console_log', {'data': msg})
        except queue.Empty:
            continue

class NappAgent:
    def __init__(self, slot):
        self.slot = slot
        self.name = AGENT_NAMES[slot-1]
        self.api_key = None
        self.headers = {}
        self.game_id = None
        self.agent_id = None
        self.last_known_game_id = None
        self.avoided_deathzone_ids = set()
        self.previous_region_id = None
        self.running = False
        self.thread = None
        self.load_key()

    def load_key(self):
        entry = ApiKey.query.filter_by(slot=self.slot).first()
        if entry:
            self.api_key = entry.api_key
            self.name = entry.name or self.name
            self.headers = {"X-API-Key": self.api_key, "Content-Type": "application/json"}
        else:
            self.api_key = None
            self.headers = {}

    def log(self, msg):
        logger.info(f"[Slot {self.slot} - {self.name}] {msg}")

    def send_action(self, action_obj, thought=""):
        try:
            payload = {
                "action": action_obj,
                "thought": {"reasoning": thought, "plannedAction": action_obj.get("type", "unknown")}
            }
            res = requests.post(
                f"{BASE_URL}/games/{self.game_id}/agents/{self.agent_id}/action",
                json=payload, headers=self.headers
            )
            return res.json() if res.ok else {"success": False, "message": res.text}
        except Exception as e:
            self.log(f"Action error: {e}")
            return {"success": False}

    def get_state(self):
        try:
            res = requests.get(
                f"{BASE_URL}/games/{self.game_id}/agents/{self.agent_id}/state",
                headers=self.headers
            )
            if res.status_code == 200:
                return res.json()["data"]
            else:
                self.log(f"State HTTP error: {res.status_code} - {res.text[:100]}")
                return None
        except Exception as e:
            self.log(f"Get state exception: {e}")
            return None

    def recover_agent_id(self, game_id):
        try:
            res = requests.get(f"{BASE_URL}/games/{game_id}/state", headers=self.headers)
            if res.status_code == 200:
                data = res.json().get("data", {})
                agents = data.get("agents", [])
                for a in agents:
                    if a.get("name") == self.name:
                        self.log(f"Recover sukses → Agent ID: {a['id']} di game {game_id}")
                        return a["id"]
                self.log(f"Nama {self.name} tidak ditemukan di game {game_id}")
            else:
                self.log(f"Gagal get state game {game_id} → status {res.status_code}")
        except Exception as e:
            self.log(f"Recover error: {e}")
        return None

    def find_and_join_game(self):
        self.log("Mulai cari atau recover game...")
        max_wait_attempts = 20
        attempt = 0

        while not self.agent_id and attempt < max_wait_attempts:
            attempt += 1
            try:
                if self.last_known_game_id:
                    self.log(f"Prioritas recover di last known game: {self.last_known_game_id}")
                    recovered_id = self.recover_agent_id(self.last_known_game_id)
                    if recovered_id:
                        self.game_id = self.last_known_game_id
                        self.agent_id = recovered_id
                        self.log("Recover berhasil di last known game → lanjut")
                        return

                res = requests.get(f"{BASE_URL}/games?status=waiting")
                games = res.json().get("data", [])

                if not games:
                    self.log(f"Tidak ada game waiting... tunggu 15 detik (attempt {attempt}/{max_wait_attempts})")
                    time.sleep(15)
                    continue

                self.game_id = games[0]["id"]
                self.log(f"Found waiting game: {self.game_id}")

                reg = requests.post(
                    f"{BASE_URL}/games/{self.game_id}/agents/register",
                    json={"name": self.name}, headers=self.headers
                )

                if reg.status_code == 201:
                    self.agent_id = reg.json()["data"]["id"]
                    self.last_known_game_id = self.game_id
                    self.log(f"Join sukses! Agent ID: {self.agent_id}")
                    return

                elif "ONE_AGENT_PER_API_KEY" in reg.text or "ACCOUNT_ALREADY_IN_GAME" in reg.text:
                    self.log("Akun sudah di game lain → coba recover")
                    try:
                        err_data = reg.json()
                        current_game = err_data.get("error", {}).get("currentGameId")
                        if current_game:
                            self.log(f"Recover dari error currentGameId: {current_game}")
                            recovered_id = self.recover_agent_id(current_game)
                            if recovered_id:
                                self.game_id = current_game
                                self.agent_id = recovered_id
                                self.last_known_game_id = self.game_id
                                self.log("Recover berhasil dari error → tunggu game start")
                                return
                    except:
                        pass

                    if self.last_known_game_id:
                        recovered_id = self.recover_agent_id(self.last_known_game_id)
                        if recovered_id:
                            self.game_id = self.last_known_game_id
                            self.agent_id = recovered_id
                            self.log("Fallback recover berhasil di last known → tunggu game start")
                            return

                else:
                    self.log(f"Register gagal: {reg.text[:100]}")

                time.sleep(15)

            except Exception as e:
                self.log(f"Find/join error: {e}")
                time.sleep(15)

        self.log("Gagal join/recover setelah max attempt → akan dicoba lagi nanti")
        # Tidak exit, biarkan loop utama handle retry

    def get_best_weapon(self, inventory):
        weapons = [i for i in inventory if i.get("category") == "weapon"]
        if not weapons:
            return None
        return max(weapons, key=lambda x: x.get("atkBonus") or x.get("atk") or x.get("damage") or x.get("power") or 0)

    def get_item_stats(self, item):
        if item is None:
            return 0
        return item.get("atkBonus") or item.get("atk") or item.get("damage") or item.get("power") or 0

    def get_safe_move_target(self, data):
        region = data["currentRegion"]
        connections = region.get("connections", [])
        if not connections:
            return None

        visible_regions = {r["id"]: r for r in data.get("visibleRegions", [])}
        pending_dz = [dz["id"] for dz in data.get("pendingDeathzones", [])]

        if region.get("isDeathZone"):
            self.avoided_deathzone_ids.add(region["id"])
        self.avoided_deathzone_ids.update(pending_dz)

        safe = []
        risky = []

        for conn_id in connections:
            is_avoided = conn_id in self.avoided_deathzone_ids
            is_current_dz = conn_id in visible_regions and visible_regions[conn_id].get("isDeathZone", False)
            is_pending = conn_id in pending_dz

            if is_current_dz or is_pending or is_avoided:
                risky.append(conn_id)
            else:
                safe.append(conn_id)

        if region.get("isDeathZone") and self.previous_region_id and self.previous_region_id in connections:
            if self.previous_region_id not in self.avoided_deathzone_ids and \
               not (self.previous_region_id in visible_regions and visible_regions[self.previous_region_id].get("isDeathZone", False)) and \
               self.previous_region_id not in pending_dz:
                self.log(f"FORCE BACKTRACK dari DZ → kembali ke previous {self.previous_region_id[:8]}...")
                target = self.previous_region_id
                self.previous_region_id = region["id"]
                return target

        if self.previous_region_id and self.previous_region_id in connections:
            if self.previous_region_id not in self.avoided_deathzone_ids and \
               not (self.previous_region_id in visible_regions and visible_regions[self.previous_region_id].get("isDeathZone", False)) and \
               self.previous_region_id not in pending_dz:
                self.log(f"Backtrack → kembali ke previous {self.previous_region_id[:8]}...")
                target = self.previous_region_id
                self.previous_region_id = region["id"]
                return target

        if safe:
            target = random.choice(safe)
            self.log(f"Safe move → {target[:8]}... ({len(safe)} opsi | avoided: {len(self.avoided_deathzone_ids)})")
            self.previous_region_id = region["id"]
            return target

        if risky:
            if len(risky) <= 2:
                target = random.choice(risky)
                self.log(f"FORCED ke risk (sisa {len(risky)} opsi) → {target[:8]}...")
            else:
                target = random.choice(risky)
                self.log(f"No safe → random risky {target[:8]}... (risky: {len(risky)})")
            self.previous_region_id = region["id"]
            return target

        target = random.choice(connections)
        self.log(f"CRITICAL: Semua avoided/risk → forced {target[:8]}...")
        self.previous_region_id = region["id"]
        return target

    def free_actions(self, data):
        me = data["self"]
        region_id = me["regionId"]
        inv = me.get("inventory", [])
        inv_count = len(inv)

        map_item = next((i for i in inv if i.get("typeId") == "map" or i.get("name") == "Map"), None)
        if map_item:
            self.log(f"Gunakan Map untuk reveal full map")
            self.send_action({"type": "use_item", "itemId": map_item["id"]}, "Reveal map hindari DZ")
            data = self.get_state() or data
            me = data["self"]
            inv = me.get("inventory", [])
            inv_count = len(inv)

        if inv_count > 8:
            equipped_weapon = me.get("equippedWeapon")
            equipped_stats = self.get_item_stats(equipped_weapon)
            droppable = [i for i in inv if i.get("category") not in ["recovery", "currency", "weapon"] or 
                         (i.get("category") == "weapon" and self.get_item_stats(i) < equipped_stats)]
            if droppable:
                drop_item = min(droppable, key=lambda x: self.get_item_stats(x) if x.get("category") == "weapon" else 999)
                self.log(f"Drop {drop_item.get('name', 'item')} untuk space weapon")
                self.send_action({"type": "drop", "itemId": drop_item["id"]}, "Buat space loot weapon")
                data = self.get_state() or data
                me = data["self"]
                inv = me.get("inventory", [])
                inv_count = len(inv)

        visible_weapons = [
            item_entry["item"]
            for item_entry in data.get("visibleItems", [])
            if item_entry.get("regionId") == region_id and item_entry["item"].get("category") == "weapon"
        ]

        if visible_weapons and inv_count < 10:
            equipped_weapon = me.get("equippedWeapon")
            current_stats = self.get_item_stats(equipped_weapon)
            best_visible = max(visible_weapons, key=lambda x: self.get_item_stats(x), default=None)

            if best_visible and self.get_item_stats(best_visible) > current_stats:
                self.log(f"PRIORITAS WEAPON → Ambil {best_visible.get('name')} (+{self.get_item_stats(best_visible)} > {current_stats})")
                self.send_action({"type": "pickup", "itemId": best_visible["id"]}, "Ambil weapon high-tier dulu")
                inv_count += 1
                data = self.get_state() or data
                me = data["self"]
                inv = me.get("inventory", [])

                equipped_after = me.get("equippedWeapon")
                if equipped_after is None or self.get_item_stats(equipped_after) < self.get_item_stats(best_visible):
                    self.log(f"FORCE EQUIP high-tier: {best_visible.get('name')}")
                    self.send_action({"type": "equip", "itemId": best_visible["id"]}, "Equip weapon high-tier langsung")
                    data = self.get_state() or data
                    me = data["self"]
                    inv = me.get("inventory", [])

        for item_entry in data.get("visibleItems", []):
            if item_entry.get("regionId") != region_id or inv_count >= 10:
                continue
            item = item_entry["item"]
            if item.get("category") == "currency" and "$Moltz" in item.get("name", ""):
                self.log(f"Ambil $Moltz ({item.get('quantity', 1)})")
                self.send_action({"type": "pickup", "itemId": item["id"]}, "Ambil currency setelah cek weapon")
                inv_count += 1
                data = self.get_state() or data
                me = data["self"]
                inv = me.get("inventory", [])

        for item_entry in data.get("visibleItems", []):
            if item_entry.get("regionId") != region_id or inv_count >= 10:
                continue
            item = item_entry["item"]
            if item.get("category") == "recovery":
                self.log(f"Pickup recovery: {item.get('name')}")
                self.send_action({"type": "pickup", "itemId": item["id"]}, "Ambil heal")
                inv_count += 1
                data = self.get_state() or data
                me = data["self"]
                inv = me.get("inventory", [])

        best = self.get_best_weapon(inv)
        if best:
            equipped = me.get("equippedWeapon")
            best_dmg = self.get_item_stats(best)
            eq_dmg = self.get_item_stats(equipped)
            is_equipped = best.get("isEquipped", False)

            if not is_equipped and (best_dmg > eq_dmg or equipped is None):
                self.log(f"Equip best: {best['name']} (+{best_dmg})")
                self.send_action({"type": "equip", "itemId": best["id"]}, "Equip weapon terbaik")

    def decide_action(self, data):
        me = data["self"]
        hp = me["hp"]
        ep = me["ep"]
        region = data["currentRegion"]
        inv = me.get("inventory", [])

        if region.get("isDeathZone"):
            self.avoided_deathzone_ids.add(region["id"])
        for dz in data.get("pendingDeathzones", []):
            self.avoided_deathzone_ids.add(dz["id"])

        if region.get("isDeathZone"):
            target = self.get_safe_move_target(data)
            if target:
                return {"type": "move", "regionId": target}, "Keluar DZ → backtrack / safe"
            return {"type": "rest"}, "Trapped DZ → rest"

        interactables = region.get("interactables", [])
        if interactables and ep >= 1:
            for fac in interactables:
                if not fac.get("isUsed", False) and "medical" in fac.get("type", "").lower() and hp <= 70:
                    self.log(f"Interact medical (HP {hp})")
                    return {"type": "interact", "interactableId": fac["id"]}, "Heal medical"

        if hp < 55:
            recovery = next((i for i in inv if i["category"] == "recovery"), None)
            if recovery and ep >= 1:
                return {"type": "use_item", "itemId": recovery["id"]}, f"Heal {recovery['name']} (HP {hp})"
            target = self.get_safe_move_target(data)
            if target:
                return {"type": "move", "regionId": target}, "HP rendah no recovery → kabur"

        if ep <= 1:
            return {"type": "rest"}, "Rest EP"

        monsters_here = [m for m in data.get("visibleMonsters", []) 
                        if m["regionId"] == me["regionId"]]

        if monsters_here and ep >= 2:
            equipped = me.get("equippedWeapon") is not None
            bandits = [m for m in monsters_here if "bandit" in m.get("name", "").lower()]
            other_monsters = [m for m in monsters_here if m not in bandits]

            if other_monsters:
                target = other_monsters[0]
                return {"type": "attack", "targetId": target["id"], "targetType": "monster"}, f"Farm non-bandit {target.get('name', 'monster')}"

            if equipped and bandits:
                target = bandits[0]
                return {"type": "attack", "targetId": target["id"], "targetType": "monster"}, f"Farm Bandit (equip OK) {target.get('name')}"

        players_here = [a for a in data.get("visibleAgents", []) 
                       if a["id"] != self.agent_id 
                       and a.get("isAlive", True) 
                       and a["regionId"] == me["regionId"]]

        bot_teammates_here = [a for a in players_here if a.get("name", "").startswith("regedx")]
        real_players_here = [a for a in players_here if not a.get("name", "").startswith("regedx")]

        if bot_teammates_here and real_players_here and ep >= 3 and me.get("equippedWeapon"):
            has_recovery = any(i["category"] == "recovery" for i in inv)
            if (hp > 70 or (hp > 50 and has_recovery)):
                candidates = []
                my_atk = me["atk"] + (me.get("equippedWeapon", {}).get("atkBonus", 0) or 0)
                my_def = me["def"]

                for p in real_players_here:
                    p_hp = p["hp"]
                    p_atk = p.get("atk", 0) + (p.get("equippedWeapon", {}).get("atkBonus", 0) if p.get("equippedWeapon") else 0)
                    p_def = p.get("def", 0)
                    est_dmg_to_p = max(1, my_atk - p_def)
                    turns_to_kill = p_hp / est_dmg_to_p if est_dmg_to_p > 0 else 999
                    threat = max(1, p_atk - my_def)
                    moltz_bonus = sum(i.get("quantity", 0) for i in p.get("inventory", []) if i.get("category") == "currency") * 2
                    
                    score = (100 - p_hp) * 3 + moltz_bonus - (turns_to_kill * 5) - (threat * 1.2)
                    if score > 25:
                        candidates.append((score, p))

                if candidates:
                    candidates.sort(reverse=True)
                    target = candidates[0][1]
                    return {"type": "attack", "targetId": target["id"], "targetType": "agent"}, \
                           f"Kerjasama bunuh player {target['name']} (HP {target['hp']}, score {candidates[0][0]:.1f})"

        target = self.get_safe_move_target(data)
        if target:
            return {"type": "move", "regionId": target}, "Cari spot farm aman"
        return {"type": "rest"}, "Rest aman"

    def run_loop(self):
        if not self.api_key:
            self.log("Tidak ada API key → loop tidak dijalankan")
            return

        self.log("Memulai run_loop...")
        self.find_and_join_game()

        while self.running:
            try:
                data = self.get_state()
                if not data:
                    self.log("State gagal → cek status game...")
                    try:
                        game_res = requests.get(f"{BASE_URL}/games/{self.game_id}")
                        if game_res.status_code == 200:
                            game_status = game_res.json().get("data", {}).get("room", {}).get("status")
                            if game_status in ["finished", "ended", "closed"]:
                                self.log(f"Game sudah finish ({game_status}) → cari game baru")
                                self.agent_id = None
                                self.find_and_join_game()
                                continue
                            elif game_status == "waiting":
                                self.log(f"Game masih waiting ({game_status}) → tunggu 15 detik")
                                time.sleep(15)
                                continue
                    except Exception as e:
                        self.log(f"Error cek game status: {e}")
                    time.sleep(10)
                    continue

                me = data["self"]
                if not me.get("isAlive", True):
                    self.log("Agent mati → cari game baru")
                    self.agent_id = None
                    self.find_and_join_game()
                    continue

                if data.get("gameStatus") == "waiting":
                    self.log("Game masih waiting → tunggu start (poll 15 detik)")
                    time.sleep(15)
                    continue

                if data.get("gameStatus") != "running":
                    self.log(f"Game status bukan running ({data.get('gameStatus')}) → cari game baru")
                    self.agent_id = None
                    self.find_and_join_game()
                    continue

                self.free_actions(data)
                if not data["currentRegion"].get("isDeathZone"):
                    self.previous_region_id = data["currentRegion"]["id"]

                action, reason = self.decide_action(data)
                result = self.send_action(action, reason)
                msg = result.get("message", "OK") if result else "Gagal"
                self.log(f"{action['type'].upper()} | {reason} | {msg}")

                time.sleep(61)

            except Exception as e:
                self.log(f"Loop error: {str(e)}")
                time.sleep(15)

        self.log("run_loop telah dihentikan")

    def start(self):
        if not self.api_key:
            self.log("No API key → skipped")
            return
        if self.running:
            self.log("Already running")
            return
        self.running = True
        self.thread = threading.Thread(target=self.run_loop, daemon=True)
        self.thread.start()
        self.log("Thread started")

    def stop(self):
        self.running = False
        self.log("Stopping... (will finish current cycle)")

# Global agents
agents = {i: NappAgent(i) for i in range(1, 6)}

@app.route('/')
def dashboard():
    entries = ApiKey.query.order_by(ApiKey.slot).all()
    key_data = {e.slot: {'key': e.api_key, 'name': e.name} for e in entries}
    return render_template('dashboard.html', keys=key_data, agents=agents)

@app.route('/save_keys', methods=['POST'])
def save_keys():
    for slot in range(1, 6):
        key = request.form.get(f'key_{slot}', '').strip()
        name = request.form.get(f'name_{slot}', f"regedx{slot}").strip()
        if key:
            entry = ApiKey.query.filter_by(slot=slot).first()
            if not entry:
                entry = ApiKey(slot=slot)
                db.session.add(entry)
            entry.api_key = key
            entry.name = name
            db.session.commit()
            agents[slot].load_key()
            agents[slot].start()
        else:
            entry = ApiKey.query.filter_by(slot=slot).first()
            if entry:
                db.session.delete(entry)
                db.session.commit()
                agents[slot].stop()
    return redirect(url_for('dashboard'))

@app.route('/generate_account', methods=['POST'])
def generate_account():
    custom_name = request.form.get('name', '').strip()
    name = custom_name or f"regedx{random.randint(1000,9999)}"

    try:
        res = requests.post(f"{BASE_URL}/accounts", json={"name": name})
        res.raise_for_status()
        data = res.json()
        api_key = data['data']['apiKey']

        used_slots = {e.slot for e in ApiKey.query.all()}
        free_slot = next((s for s in range(1,6) if s not in used_slots), None)

        if not free_slot:
            return jsonify({'success': False, 'message': 'Semua 5 slot sudah terisi!'})

        entry = ApiKey(slot=free_slot, api_key=api_key, name=name)
        db.session.add(entry)
        db.session.commit()

        agents[free_slot].load_key()
        agents[free_slot].start()

        return jsonify({'success': True, 'message': f'Akun {name} dibuat! Key disimpan di slot {free_slot}'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Gagal create account: {str(e)}'})

@socketio.on('connect')
def handle_connect():
    emit('console_log', {'data': '=== Connected to live console ==='})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()

    threading.Thread(target=broadcast_logs, daemon=True).start()

    for agent in agents.values():
        if agent.api_key:
            agent.start()

    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)