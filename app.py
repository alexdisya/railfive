import os
import time
import threading
from queue import Queue
import requests
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO
import logging

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'ganti-dengan-rahasia-yang-kuat-123456')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Queue untuk mengirim log ke semua client secara real-time
log_queue = Queue()

def log_worker():
    while True:
        try:
            message = log_queue.get(timeout=1)
            socketio.emit('log', {'message': message})
        except:
            time.sleep(0.1)

threading.Thread(target=log_worker, daemon=True).start()

# Redirect semua print() dan error ke queue → muncul di dashboard
class WebLogger:
    def write(self, text):
        if text and text.strip():
            log_queue.put(text.strip())
    def flush(self):
        pass

import sys
sys.stdout = WebLogger()
sys.stderr = WebLogger()

# Konfigurasi bot (diubah dari web)
bot_config = {
    'api_key': '',
    'agent_id': '',
    'cooldown_seconds': 60,
    'running': False,
    'stop_event': threading.Event(),
}

bot_thread = None

class MoltArenaBot:
    def __init__(self, config):
        self.api_key = config['api_key']
        self.agent_id = config['agent_id']
        self.cooldown_seconds = config['cooldown_seconds']
        
        if not self.api_key or not self.agent_id:
            raise ValueError("API Key dan Agent ID harus diisi!")

        self.base_url = "https://moltarena.crosstoken.io/api"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Moltbot-MoltArena-Skill/1.0"
        }

    def create_battle(self):
        payload = {
            "agent1Id": self.agent_id,
            "rounds": 5,
            "language": "en",
            "visibility": "public"
        }

        max_retries_500 = 5
        retry_count_500 = 0

        while retry_count_500 < max_retries_500:
            try:
                resp = requests.post(
                    f"{self.base_url}/deploy/battle",
                    headers=self.headers,
                    json=payload,
                    timeout=15
                )

                if resp.status_code == 201:
                    data = resp.json()
                    if data.get("success"):
                        battle_id = data["battle"]["id"]
                        print(f"Battle dibuat! ID: {battle_id} (attempt {retry_count_500 + 1})")
                        return battle_id
                    else:
                        print(f"Response success=false: {data}")
                        return None

                elif resp.status_code == 429:
                    try:
                        error_data = resp.json()
                        retry_after = error_data.get("retryAfter", 30)
                        wait_sec = float(retry_after)
                    except:
                        wait_sec = 30

                    print(f"Rate limit (429) → tunggu {wait_sec:.0f} detik ...")
                    time.sleep(wait_sec)
                    continue

                elif resp.status_code == 500:
                    retry_count_500 += 1
                    wait_sec = 5 * (retry_count_500 ** 1.5)
                    print(f"Server error 500 (attempt {retry_count_500}/{max_retries_500}) → tunggu {wait_sec:.1f} detik")
                    time.sleep(wait_sec)
                    continue

                else:
                    print(f"Gagal create battle - status {resp.status_code}: {resp.text[:200]}")
                    return None

            except Exception as e:
                print(f"Request gagal: {e}")
                time.sleep(8)
                continue

        print(f"Gagal setelah {max_retries_500} percobaan 500.")
        return None

    def get_battle_status(self, battle_id):
        try:
            resp = requests.get(
                f"{self.base_url}/battles/{battle_id}",
                headers=self.headers,
                timeout=12
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"Gagal ambil status battle {battle_id}: {e}")
            return None

    def vote(self, battle_id):
        payload = {"agentId": self.agent_id}
        try:
            resp = requests.post(
                f"{self.base_url}/battles/{battle_id}/vote",
                headers=self.headers,
                json=payload,
                timeout=10
            )
            if resp.status_code in (200, 201):
                print(f"Vote berhasil untuk agent {self.agent_id}!")
            else:
                print(f"Vote gagal: {resp.status_code} - {resp.text}")
        except Exception as e:
            print(f"Error voting: {e}")

    def print_card(self, battle_data, rounds_data=None):
        battle = battle_data.get("battle", {})
        agent_a = battle.get("agentA", {})
        agent_b = battle.get("agentB", {})

        my_is_a = agent_a.get("id") == self.agent_id
        my_name   = agent_a.get("displayName", "???") if my_is_a else agent_b.get("displayName", "???")
        opp_name  = agent_b.get("displayName", "???") if my_is_a else agent_a.get("displayName", "???")
        my_rating = agent_a.get("rating", "?") if my_is_a else agent_b.get("rating", "?")
        opp_rating= agent_b.get("rating", "?") if my_is_a else agent_a.get("rating", "?")
        topic     = battle.get("topic", "—")
        status    = battle.get("status", "unknown").upper()
        current   = battle.get("currentRound", "?")
        total     = battle.get("totalRounds", "?")

        print(f"╔════════════════════════════════════════════════════════════╗")
        print(f"║               MOLT ARENA BATTLE #{battle.get('battleNumber', '?')}               ║")
        print(f"╠════════════════════════════════════════════════════════════╣")
        print(f"║  {my_name} ({my_rating})  vs  {opp_name} ({opp_rating})")
        print(f"║  Topic : {topic}")
        print(f"║  Status: {status}")
        print(f"║  Round : {current}/{total}")

        if rounds_data:
            icons = []
            wits_my = []
            wits_opp = []

            for rnd in rounds_data:
                msg_a = rnd.get("agentAMessage") or {}
                msg_b = rnd.get("agentBMessage") or {}

                my_w   = msg_a.get("witScore") if my_is_a else msg_b.get("witScore")
                opp_w  = msg_b.get("witScore") if my_is_a else msg_a.get("witScore")

                if my_w is None or opp_w is None:
                    icons.append("…")
                    wits_my.append("?")
                    wits_opp.append("?")
                else:
                    try:
                        my_f = float(my_w)
                        opp_f = float(opp_w)
                        diff = my_f - opp_f

                        if abs(diff) < 0.3:
                            icons.append("⚪")
                        elif diff > 0:
                            icons.append("🟢")
                        else:
                            icons.append("🔴")

                        wits_my.append(f"{my_f:.2f}")
                        wits_opp.append(f"{opp_f:.2f}")
                    except:
                        icons.append("⚪")
                        wits_my.append("?")
                        wits_opp.append("?")

            print(f"║  Rounds: {' '.join(icons)}")
            if any(w != "?" for w in wits_my):
                print(f"║  You : {' '.join([f'{w:>4}' for w in wits_my])}")
                print(f"║  Opp : {' '.join([f'{w:>4}' for w in wits_opp])}")
        else:
            print("║  Waiting for first round...")

        print(f"╚════════════════════════════════════════════════════════════╝")
        print("")

    def run_loop(self):
        print("Bot dimulai...")
        print(f"Agent ID : {self.agent_id}")
        print(f"Cooldown  : {self.cooldown_seconds} detik\n")

        while not bot_config['stop_event'].is_set():
            battle_id = self.create_battle()
            if not battle_id:
                print("Gagal membuat battle → tunggu 30 detik...")
                time.sleep(30)
                continue

            last_status = None
            last_rounds_shown = 0

            while not bot_config['stop_event'].is_set():
                data = self.get_battle_status(battle_id)
                if not data:
                    time.sleep(10)
                    continue

                battle = data.get("battle", {})
                status = battle.get("status", "").lower()
                current_round = battle.get("currentRound", 0)
                rounds = data.get("rounds", [])

                should_redraw = (
                    len(rounds) > last_rounds_shown or
                    status != last_status or
                    current_round != battle.get("currentRound", 0)
                )

                if should_redraw:
                    self.print_card(data, rounds)
                    last_rounds_shown = len(rounds)
                    last_status = status

                if status == "completed":
                    winner_id = battle.get("winnerId")
                    if winner_id == self.agent_id:
                        print("★ VICTORY! Agentmu MENANG! ★\n")
                    else:
                        print("✗ DEFEAT ✗\n")
                    break

                # Uncomment jika ingin auto vote
                # elif status in ("voting", "vote"):
                #     print("Voting phase → vote otomatis...")
                #     self.vote(battle_id)
                #     time.sleep(5)

                time.sleep(10 + current_round * 1.5)

            if not bot_config['stop_event'].is_set():
                print(f"Battle selesai. Cooldown {self.cooldown_seconds} detik...")
                remaining = self.cooldown_seconds
                while remaining > 0 and not bot_config['stop_event'].is_set():
                    mins, secs = divmod(remaining, 60)
                    print(f"\rMenunggu berikutnya: {mins:02d}:{secs:02d} ", end="", flush=True)
                    time.sleep(1)
                    remaining -= 1
                print("\rCooldown selesai! Membuat battle baru...          ")

        print("Bot dihentikan oleh pengguna.")

# ────────────────────────────────────────────────
# Routes Flask

@app.route('/')
def dashboard():
    return render_template('dashboard.html', config=bot_config)

@app.route('/api/update_config', methods=['POST'])
def update_config():
    data = request.json
    bot_config['api_key'] = data.get('api_key', '').strip()
    bot_config['agent_id'] = data.get('agent_id', '').strip()
    bot_config['cooldown_seconds'] = int(data.get('cooldown', 60))
    print("Konfigurasi diperbarui dari dashboard.")
    return jsonify({'status': 'updated'})

@app.route('/api/start_bot', methods=['POST'])
def start_bot():
    global bot_thread

    if bot_config['running']:
        return jsonify({'status': 'already_running'})

    if not bot_config['api_key'] or not bot_config['agent_id']:
        return jsonify({'status': 'error', 'message': 'API Key dan Agent ID harus diisi!'})

    bot_config['running'] = True
    bot_config['stop_event'].clear()

    bot = MoltArenaBot(bot_config)
    bot_thread = threading.Thread(target=bot.run_loop, daemon=True)
    bot_thread.start()

    print("Bot di-start dari dashboard.")
    return jsonify({'status': 'started'})

@app.route('/api/stop_bot', methods=['POST'])
def stop_bot():
    if not bot_config['running']:
        return jsonify({'status': 'not_running'})

    bot_config['running'] = False
    bot_config['stop_event'].set()
    print("Permintaan stop bot diterima. Menunggu loop selesai...")
    return jsonify({'status': 'stopping'})

if __name__ == '__main__':
    port = int(os.getenv('PORT', 8080))
    print(f"Server Flask-SocketIO berjalan di port {port}")
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)