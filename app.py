import sqlite3
import json
import datetime
import os
import socket
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from openai import OpenAI

# --- CONFIGURATION ---
DEEPSEEK_API_KEY = "sk-60b3c998b7bc4a1e9de1175de78c3e75" 
client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect('points.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS scores
                 (id INTEGER PRIMARY KEY, date TEXT UNIQUE, points INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS tasks
                 (id INTEGER PRIMARY KEY, name TEXT, points INTEGER, category TEXT, completed_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS media_logs
                 (id INTEGER PRIMARY KEY, date TEXT, task_name TEXT, points_deducted INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS daily_schedules
                 (id INTEGER PRIMARY KEY, date TEXT UNIQUE, schedule_json TEXT, completed_items TEXT DEFAULT '[]')''')
    conn.commit()
    conn.close()

init_db()

# --- DATA MODELS ---
class ScheduleInput(BaseModel):
    dos: str
    donts: str

class TaskComplete(BaseModel):
    task_name: str
    points: int

class NegativeTaskLog(BaseModel):
    task_name: str
    penalty_applied: int

# --- HELPER: Get Local IP for Sharing ---
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "localhost"

# --- DEEPSEEK PROMPT ENGINEERING ---
def generate_schedule_with_deepseek(dos_text, donts_text):
    prompt = f"""
    I am Shawn. My top 10 CliftonStrengths are: Achiever, Context, Adaptability, Developer, Learner, Futuristic, Competition, Positivity, Arranger, Self-Assurance.
    I have a list of "Dos" and "Don'ts":
    Dos: "{dos_text}"
    Don'ts: "{donts_text}"
    Generate a schedule. 
    - For every "Do", assign a positive point value (1 to 10).
    - For every "Don't", assign a negative point value (-1 to -10) based on how harmful it is. Be harsh but fair.
    Return result strictly in JSON with keys:
    - "tasks": A list of objects. For "Dos": {{"name": "str", "type": "positive", "points": int, "duration_mins": int}}. For "Don'ts": {{"name": "str", "type": "negative", "points": int}}.
    - "buffer_time": int.
    - "ideal_score": int (sum of all positive points, ignore negatives).
    Just raw JSON. No markdown.
    """

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "You are a strict, fair productivity coach."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
        )
        json_string = response.choices[0].message.content.strip()
        if json_string.startswith("```json"):
            json_string = json_string[7:-3]
        return json.loads(json_string)
    except Exception as e:
        print(f"DeepSeek Error: {e}")
        dos_list = [{"name": t.strip(), "type": "positive", "points": 5, "duration_mins": 60} for t in dos_text.split(",")]
        donts_list = [{"name": t.strip(), "type": "negative", "points": -5} for t in donts_text.split(",")]
        return {"tasks": dos_list + donts_list, "buffer_time": 10, "ideal_score": 15}

# --- API ENDPOINTS ---
@app.post("/api/generate_schedule")
async def get_schedule(data: ScheduleInput):
    schedule = generate_schedule_with_deepseek(data.dos, data.donts)
    today = datetime.date.today().isoformat()
    conn = sqlite3.connect('points.db')
    c = conn.cursor()
    c.execute("INSERT INTO daily_schedules (date, schedule_json, completed_items) VALUES (?, ?, ?) ON CONFLICT(date) DO UPDATE SET schedule_json = ?, completed_items = '[]'", (today, json.dumps(schedule), json.dumps([]), json.dumps(schedule)))
    conn.commit()
    conn.close()
    return JSONResponse(content=schedule)

@app.get("/api/get_todays_schedule")
async def get_todays_schedule():
    today = datetime.date.today().isoformat()
    conn = sqlite3.connect('points.db')
    c = conn.cursor()
    c.execute("SELECT schedule_json, completed_items FROM daily_schedules WHERE date = ?", (today,))
    res = c.fetchone()
    conn.close()
    if res:
        return JSONResponse(content={
            "schedule": json.loads(res[0]),
            "completed": json.loads(res[1])
        })
    return JSONResponse(content={"schedule": {"tasks": [], "ideal_score": 0}, "completed": []})

@app.post("/api/complete_task")
async def complete_task(data: TaskComplete):
    today = datetime.date.today().isoformat()
    conn = sqlite3.connect('points.db')
    c = conn.cursor()
    c.execute("INSERT INTO scores (date, points) VALUES (?, ?) ON CONFLICT(date) DO UPDATE SET points = points + ?", (today, data.points, data.points))
    c.execute("SELECT completed_items FROM daily_schedules WHERE date = ?", (today,))
    res = c.fetchone()
    if res:
        completed = json.loads(res[0])
        if data.task_name not in completed:
            completed.append(data.task_name)
            c.execute("UPDATE daily_schedules SET completed_items = ? WHERE date = ?", (json.dumps(completed), today))
    c.execute("INSERT INTO tasks (name, points, category, completed_at) VALUES (?, ?, ?, ?)", (data.task_name, data.points, "Completed", datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return {"status": "success", "added": data.points}

@app.post("/api/log_negative")
async def log_negative(data: NegativeTaskLog):
    today = datetime.date.today().isoformat()
    conn = sqlite3.connect('points.db')
    c = conn.cursor()
    # DEDUCT POINTS
    c.execute("INSERT INTO scores (date, points) VALUES (?, ?) ON CONFLICT(date) DO UPDATE SET points = points + ?", (today, data.penalty_applied, data.penalty_applied))
    c.execute("SELECT completed_items FROM daily_schedules WHERE date = ?", (today,))
    res = c.fetchone()
    if res:
        completed = json.loads(res[0])
        if data.task_name not in completed:
            completed.append(data.task_name)
            c.execute("UPDATE daily_schedules SET completed_items = ? WHERE date = ?", (json.dumps(completed), today))
    c.execute("INSERT INTO media_logs (date, task_name, points_deducted) VALUES (?, ?, ?)", (today, data.task_name, data.penalty_applied))
    conn.commit()
    conn.close()
    return {"status": "logged", "deducted": data.penalty_applied}

@app.post("/api/reset_score")
async def reset_score():
    today = datetime.date.today().isoformat()
    conn = sqlite3.connect('points.db')
    c = conn.cursor()
    # Reset today's score to 0
    c.execute("INSERT INTO scores (date, points) VALUES (?, ?) ON CONFLICT(date) DO UPDATE SET points = 0", (today, 0))
    # Reset the completed items list to empty (makes all buttons active again)
    c.execute("UPDATE daily_schedules SET completed_items = '[]' WHERE date = ?", (today,))
    conn.commit()
    conn.close()
    return {"status": "reset"}

@app.get("/api/get_daily_score")
async def get_daily_score():
    today = datetime.date.today().isoformat()
    conn = sqlite3.connect('points.db')
    c = conn.cursor()
    c.execute("SELECT points FROM scores WHERE date = ?", (today,))
    res = c.fetchone()
    current = res[0] if res else 0
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    c.execute("SELECT points FROM scores WHERE date = ?", (yesterday,))
    res_y = c.fetchone()
    yesterday_score = res_y[0] if res_y else 0
    conn.close()
    return {"today_score": current, "yesterday_score": yesterday_score, "date": today}

# --- FRONTEND ---
@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    local_ip = get_local_ip()
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Shawn's Adaptive Achiever</title>
        <style>
            body {{ font-family: 'Segoe UI', sans-serif; background: #f4f7f6; padding: 20px; max-width: 800px; margin: auto; }}
            .card {{ background: white; border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.05); }}
            h1 {{ color: #2c3e50; font-size: 24px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }}
            input, textarea {{ width: 100%; padding: 10px; margin: 8px 0; border: 1px solid #ddd; border-radius: 8px; font-size: 16px; box-sizing: border-box; }}
            .btn {{ background: #3498db; color: white; border: none; padding: 12px 20px; border-radius: 8px; font-size: 16px; cursor: pointer; width: 100%; }}
            .btn-danger {{ background: #e74c3c; }}
            .btn-success {{ background: #2ecc71; }}
            .btn-secondary {{ background: #95a5a6; width: auto; padding: 8px 15px; font-size: 14px; }}
            .btn-warning {{ background: #f39c12; color: white; width: auto; padding: 8px 15px; font-size: 14px; }}
            .btn-success:disabled {{ background: #bdc3c7; color: #7f8c8d; cursor: not-allowed; }}
            .btn-danger:disabled {{ background: #f5b7b1; color: #7f8c8d; cursor: not-allowed; }}
            .task-block {{ display: flex; justify-content: space-between; padding: 10px; border-bottom: 1px solid #eee; align-items: center; flex-wrap: wrap; gap: 10px; }}
            .score-box {{ display: flex; justify-content: space-around; font-size: 20px; text-align: center; flex-wrap: wrap; }}
            .score-val {{ font-weight: bold; font-size: 28px; color: #2c3e50; }}
            #start-new-day-btn, #share-btn, #reset-score-btn {{ width: auto; margin-top: 10px; }}
            .neg-tag {{ color: #e74c3c; font-weight: bold; }}
            #share-toast {{ position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #2c3e50; color: white; padding: 15px 25px; border-radius: 8px; display: none; z-index: 1000; }}
        </style>
    </head>
    <body>
        <div id="share-toast">Link copied to clipboard! Paste it on your phone browser.</div>

        <div class="card">
            <h1>
                <span>📈 Shawn's Adaptive Achiever</span>
                <div style="display:flex; gap:10px; flex-wrap:wrap;">
                    <button class="btn btn-warning" id="reset-score-btn" onclick="resetScore()">🔁 Reset Score & Buttons</button>
                    <button class="btn btn-secondary" id="share-btn" onclick="shareLink()">📱 Share Link</button>
                    <button class="btn btn-secondary" id="start-new-day-btn" onclick="resetDay()">Start New Day</button>
                </div>
            </h1>
            <div class="score-box">
                <div>Today: <span id="today-score" class="score-val">0</span></div>
                <div>Yesterday: <span id="yesterday-score" class="score-val">0</span></div>
            </div>
        </div>

        <div class="card" id="input-container">
            <h3>Step 1: Set Up Your Day</h3>
            <label>Things to DO (Positive Tasks - comma separated):</label>
            <textarea id="dos-input" rows="2" placeholder="e.g. Study Python, Write Report, Exercise"></textarea>
            <label>Things to AVOID (Don'ts - comma separated):</label>
            <textarea id="donts-input" rows="2" placeholder="e.g. YouTube, Instagram, Procrastinating"></textarea>
            <button class="btn" onclick="generateSchedule()">⚡ Generate My Schedule</button>
        </div>

        <div class="card" id="schedule-container" style="display:none;">
            <h3>Target Score: <span id="ideal-score"></span></h3>
            
            <div id="task-list-pos"></div>
            <hr style="border: 1px dashed #e74c3c;">
            <div id="task-list-neg"></div>
            
            <hr>
            <p style="font-size:12px; color:#7f8c8d; text-align:center;">Click <b>"Done"</b> to earn points. Click <b>"Rule Break"</b> to lose points.</p>
        </div>

        <script>
            // --- GLOBAL EVENT DELEGATION ---
            document.addEventListener('DOMContentLoaded', function() {{
                const container = document.getElementById('schedule-container');
                
                container.addEventListener('click', function(e) {{
                    const doneBtn = e.target.closest('.done-btn');
                    if (doneBtn && !doneBtn.disabled) {{
                        completeTask(doneBtn, doneBtn.dataset.name, parseInt(doneBtn.dataset.points));
                        return;
                    }}

                    const breakBtn = e.target.closest('.break-btn');
                    if (breakBtn && !breakBtn.disabled) {{
                        breakRule(breakBtn, breakBtn.dataset.name, parseInt(breakBtn.dataset.points));
                        return;
                    }}
                }});

                loadSavedSchedule();
            }});

            async function loadSavedSchedule() {{
                let response = await fetch('/api/get_todays_schedule');
                let data = await response.json();
                
                if (data.schedule && data.schedule.tasks && data.schedule.tasks.length > 0) {{
                    renderSchedule(data.schedule, data.completed || []);
                }}
                fetchScore();
            }}

            async function fetchScore() {{
                let r = await fetch('/api/get_daily_score');
                let d = await r.json();
                document.getElementById('today-score').innerText = d.today_score;
                document.getElementById('yesterday-score').innerText = d.yesterday_score;
            }}

            function renderSchedule(schedule, completedItems) {{
                document.getElementById('ideal-score').innerText = schedule.ideal_score;
                document.getElementById('schedule-container').style.display = 'block';
                document.getElementById('input-container').style.display = 'none';
                
                let htmlPos = '<h4>Dos (Must Do):</h4>';
                let htmlNeg = '<h4>Don\\'ts (Must Avoid):</h4>';

                schedule.tasks.forEach(t => {{
                    // Check if task is completed based on exact text name match
                    const isCompleted = completedItems.includes(t.name);
                    
                    if(t.type === 'positive') {{
                        htmlPos += `<div class="task-block">
                            <div><b>${{t.name}}</b> - ${{t.duration_mins}} mins</div>
                            <div><span style="font-weight:bold;">+${{t.points}} pts</span> 
                            <button class="btn btn-success done-btn" style="width:auto; padding:5px 10px; margin-left:10px;" data-name="${{t.name}}" data-points="${{t.points}}" ${{isCompleted ? 'disabled' : ''}}>${{isCompleted ? 'Done ✓' : 'Done'}}</button></div>
                        </div>`;
                    }} else {{
                        htmlNeg += `<div class="task-block">
                            <div><b>${{t.name}}</b> <span class="neg-tag">(${{t.points}} pts)</span></div>
                            <div><button class="btn btn-danger break-btn" style="width:auto; padding:5px 10px;" data-name="${{t.name}}" data-points="${{t.points}}" ${{isCompleted ? 'disabled' : ''}}>${{isCompleted ? 'Broke it!' : 'Rule Break'}}</button></div>
                        </div>`;
                    }}
                }});
                document.getElementById('task-list-pos').innerHTML = htmlPos;
                document.getElementById('task-list-neg').innerHTML = htmlNeg;
            }}

            async function generateSchedule() {{
                try {{
                    let dos = document.getElementById('dos-input').value;
                    let donts = document.getElementById('donts-input').value;

                    let response = await fetch('/api/generate_schedule', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{dos: dos, donts: donts}})
                    }});
                    let data = await response.json();
                    renderSchedule(data, []);
                    fetchScore();

                }} catch (error) {{
                    console.error("Detailed JS Error:", error);
                    alert("Something went wrong. Please check the terminal.");
                }}
            }}

            async function completeTask(btnElement, name, points) {{
                btnElement.disabled = true;
                btnElement.innerText = "Done ✓";
                await fetch('/api/complete_task', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{task_name: name, points: points}})
                }});
                await fetchScore();
            }}

            async function breakRule(btnElement, name, points) {{
                btnElement.disabled = true;
                btnElement.innerText = "Broke it!";
                await fetch('/api/log_negative', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{task_name: name, penalty_applied: points}})
                }});
                await fetchScore();
            }}

            async function resetScore() {{
                if(confirm("Reset today's score to 0 AND re-enable all buttons?")) {{
                    await fetch('/api/reset_score', {{ method: 'POST' }});
                    // Reload the schedule to re-render buttons as active
                    await loadSavedSchedule(); 
                }}
            }}

            function resetDay() {{
                if(confirm("This will clear your schedule and start a fresh day. Proceed?")) {{
                    document.getElementById('schedule-container').style.display = 'none';
                    document.getElementById('input-container').style.display = 'block';
                    document.getElementById('task-list-pos').innerHTML = '';
                    document.getElementById('task-list-neg').innerHTML = '';
                    document.getElementById('ideal-score').innerText = '';
                    document.getElementById('today-score').innerText = "0";
                    fetchScore();
                    window.scrollTo({{ top: 0, behavior: 'smooth' }});
                }}
            }}

            function shareLink() {{
                const url = 'http://{local_ip}:8000';
                navigator.clipboard.writeText(url).then(() => {{
                    const toast = document.getElementById('share-toast');
                    toast.style.display = 'block';
                    setTimeout(() => {{ toast.style.display = 'none'; }}, 3000);
                }});
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
