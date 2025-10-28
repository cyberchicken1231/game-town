# server/main.py
import json
import uuid
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app = FastAPI()

# Serve the static frontend folder under /static to avoid intercepting WebSocket paths
app.mount("/static", StaticFiles(directory="./static"), name="static")

@app.get("/")
async def index():
    # Serve the SPA entrypoint from the static folder
    return FileResponse("./static/index.html")

# In-memory state (for prototype)
players = {}  # player_id -> {ws, name, x, y, zone}
connections = {}  # player_id -> websocket
pending_games = {}  # host_id -> {game, zone, host, participants: [player_id,...]}
active_games = {}  # host_id -> {state, task}

# Broadcast convenience
async def broadcast(message: dict, exclude: str | None = None):
    data = json.dumps(message)
    to_remove = []
    for pid, ws in connections.items():
        if pid == exclude:
            continue
        try:
            await ws.send_text(data)
        except Exception:
            to_remove.append(pid)
    for pid in to_remove:
        connections.pop(pid, None)
        players.pop(pid, None)


async def send_to_players(player_ids: list, message: dict):
    data = json.dumps(message)
    to_remove = []
    for pid in player_ids:
        ws = connections.get(pid)
        if not ws:
            to_remove.append(pid)
            continue
        try:
            await ws.send_text(data)
        except Exception:
            to_remove.append(pid)
    for pid in to_remove:
        connections.pop(pid, None)
        players.pop(pid, None)

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    player_id = str(uuid.uuid4())

    try:
        # initial handshake: wait for a unique "join" with name or auto-assign
        name = None
        while True:
            text = await ws.receive_text()
            msg = json.loads(text)
            if msg.get("type") == "join":
                proposed = (msg.get("name") or f"Player-{player_id[:4]}").strip()
                # check duplicates
                duplicate = any(p.get('name') == proposed for p in players.values())
                if duplicate:
                    # reject and ask client to pick another
                    await ws.send_text(json.dumps({"type": "join_rejected", "reason": "duplicate", "suggestion": proposed + "-1"}))
                    continue
                name = proposed
                break
            else:
                # if no join, auto-assign a name if possible (ensure unique)
                auto = f"Player-{player_id[:4]}"
                if any(p.get('name') == auto for p in players.values()):
                    auto = auto + '-' + str(uuid.uuid4())[:4]
                name = auto
                break

        # default spawn
        player = {"id": player_id, "name": name, "x": 100, "y": 100, "zone": "town"}
        players[player_id] = player
        connections[player_id] = ws

        # send back assigned id + current world snapshot
        await ws.send_text(json.dumps({"type": "welcome", "you": player, "players": list(players.values())}))

        # announce to others
        await broadcast({"type": "player_joined", "player": player}, exclude=player_id)

        # main loop
        while True:
            text = await ws.receive_text()
            msg = json.loads(text)

            if msg["type"] == "move":
                # client sends new x,y
                x = float(msg.get("x", player["x"]))
                y = float(msg.get("y", player["y"]))
                player["x"] = x
                player["y"] = y
                players[player_id] = player
                await broadcast({"type": "player_moved", "player": player}, exclude=None)

            elif msg["type"] == "chat":
                content = msg.get("content", "")[:1000]
                # Only broadcast to nearby players (within 200 units)
                nearby_players = [
                    pid for pid, p in players.items()
                    if pid != player_id and
                    ((p["x"] - player["x"]) ** 2 + (p["y"] - player["y"]) ** 2) <= 200 ** 2
                ]
                await send_to_players(nearby_players, {
                    "type": "chat",
                    "from": player["name"],
                    "content": content
                })
                # Also send to the sender
                await ws.send_text(json.dumps({
                    "type": "chat",
                    "from": player["name"],
                    "content": content
                }))

            elif msg["type"] == "global_chat":
                content = msg.get("content", "")[:1000]
                await broadcast({
                    "type": "global_chat",
                    "from": player["name"],
                    "content": content
                }, exclude=None)

            elif msg["type"] == "start_game":
                # message to request launching a minigame at a location
                game = msg.get("game", "minigame")
                zone = msg.get("zone", "arcade")
                # Special-case: breakout is single-player — start immediately for host only
                if game == 'breakout':
                    # send game_started only to the host websocket so they open the breakout page
                    await ws.send_text(json.dumps({"type": "game_started", "host": player, "game": game, "zone": zone, "participants": [player]}))
                    continue
                # if more than 1 player, send an invite to others instead of starting immediately
                other_count = len(players) - 1
                if other_count > 0:
                    # create pending invite
                    pending_games[player_id] = {"game": game, "zone": zone, "host": player, "participants": [player_id]}
                    print(f"[invite] host={player_id} game={game} zone={zone} others={other_count}")
                    # notify other players with an invite
                    await broadcast({"type": "game_invite", "host": player, "game": game, "zone": zone}, exclude=player_id)
                    # inform host that invite was sent
                    await ws.send_text(json.dumps({"type": "game_pending", "host": player, "game": game, "zone": zone}))
                else:
                    # no one else — start immediately
                    await broadcast({"type": "game_started", "host": player, "game": game, "zone": zone, "participants": [player]}, exclude=None)

            elif msg["type"] == "join_game":
                # a player accepted an invite; msg should contain host_id
                host_id = msg.get("host_id")
                if host_id and host_id in pending_games:
                    pending = pending_games.pop(host_id)
                    print(f"[join] player={player_id} joining host={host_id}")
                    # create two-player participants: host + this joiner
                    host_pid = host_id
                    joiner_pid = player_id
                    participants_ids = [host_pid, joiner_pid]
                    # build participant objects and assign sides (host=left, joiner=right)
                    participants = []
                    for idx, pid in enumerate(participants_ids):
                        p = players.get(pid)
                        if not p:
                            continue
                        side = 'left' if idx == 0 else 'right'
                        # copy and annotate side
                        part = dict(p)
                        part['side'] = side
                        participants.append(part)
                    # broadcast game started to all (could restrict to participants)
                    print(f"[start] participants={[p['id'] for p in participants]}")
                    await broadcast({"type": "game_started", "host": pending["host"], "game": pending["game"], "zone": pending["zone"], "participants": participants}, exclude=None)

                    # create an active authoritative game and start its loop
                    host_pid = host_pid if 'host_pid' in locals() else participants[0]['id']
                    participant_ids = [p['id'] for p in participants]
                    # initialize game state
                    game_state = {
                        'host': pending['host'],
                        'host_id': host_pid,
                        'participants': participant_ids,
                        'ball': {'x': 400.0, 'y': 250.0, 'vx': 5.0, 'vy': 0.0, 'r': 9},
                        'paddles': {'left': {'y': 200.0}, 'right': {'y': 200.0}},
                        'scores': {'left': 0, 'right': 0},
                        'winTo': 7,
                    }
                    async def game_loop(host_id, state_dict):
                        print(f"[game_loop] starting for host={host_id}")
                        try:
                            tick = 1/30
                            while True:
                                # physics
                                b = state_dict['ball']
                                b['x'] += b['vx']
                                b['y'] += b['vy']
                                # top/bottom bounce
                                if b['y'] - b['r'] <= 0:
                                    b['y'] = b['r']; b['vy'] *= -1
                                if b['y'] + b['r'] >= 500:
                                    b['y'] = 500 - b['r']; b['vy'] *= -1
                                # paddle collisions
                                left_x = 30 + 12
                                right_x = 800 - 42
                                ly = state_dict['paddles']['left']['y']
                                ry = state_dict['paddles']['right']['y']
                                # left paddle collision
                                if b['x'] - b['r'] <= left_x:
                                    if b['y'] > ly and b['y'] < ly + 100:
                                        b['x'] = left_x + b['r']
                                        rel = (b['y'] - (ly + 50)) / 50
                                        speed = (abs(b['vx']) + abs(b['vy'])) * 1.05 or 5.0
                                        angle = rel * (3.1415/3)
                                        b['vx'] = abs(speed) * (1.0)
                                        b['vy'] = speed * (0.5 * rel)
                                # right paddle collision
                                if b['x'] + b['r'] >= right_x:
                                    if b['y'] > ry and b['y'] < ry + 100:
                                        b['x'] = right_x - b['r']
                                        rel = (b['y'] - (ry + 50)) / 50
                                        speed = (abs(b['vx']) + abs(b['vy'])) * 1.05 or 5.0
                                        angle = rel * (3.1415/3)
                                        b['vx'] = -abs(speed) * (1.0)
                                        b['vy'] = speed * (0.5 * rel)
                                # scoring
                                if b['x'] < 0:
                                    state_dict['scores']['right'] += 1
                                    # reset ball toward scoring side
                                    b.update({'x':400.0,'y':250.0,'vx':5.0,'vy':0.0})
                                if b['x'] > 800:
                                    state_dict['scores']['left'] += 1
                                    b.update({'x':400.0,'y':250.0,'vx':-5.0,'vy':0.0})

                                # broadcast state to participants
                                try:
                                    await send_to_players(participant_ids, {'type':'game_state','host_id': host_id, 'ball': b, 'paddles': state_dict['paddles'], 'scores': state_dict['scores']})
                                except Exception:
                                    pass

                                # win
                                if state_dict['scores']['left'] >= state_dict['winTo'] or state_dict['scores']['right'] >= state_dict['winTo']:
                                    await send_to_players(participant_ids, {'type':'game_over','scores': state_dict['scores']})
                                    break

                                await asyncio.sleep(tick)
                        finally:
                            print(f"[game_loop] ending for host={host_id}")

                    task = asyncio.create_task(game_loop(host_pid, game_state))
                    active_games[host_pid] = {'state': game_state, 'task': task}

            else:
                # unknown message ignored
                pass

            # handle paddle input proxied from lobby (from minigame window)
            if msg.get('type') == 'paddle_move':
                # payload: host_id, side, y
                host_id = msg.get('host_id')
                side = msg.get('side')
                y = float(msg.get('y', 0))
                if host_id and host_id in active_games:
                    ag = active_games[host_id]['state']
                    if side == 'left':
                        ag['paddles']['left']['y'] = max(0, min(500-100, y))
                    else:
                        ag['paddles']['right']['y'] = max(0, min(500-100, y))

    except WebSocketDisconnect:
        # cleanup
        connections.pop(player_id, None)
        players.pop(player_id, None)
        await broadcast({"type": "player_left", "id": player_id})
    except Exception as e:
        # on error, try cleanup too
        connections.pop(player_id, None)
        players.pop(player_id, None)
        await broadcast({"type": "player_left", "id": player_id})
        # log
        print("Websocket error:", e)
