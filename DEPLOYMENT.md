# Xira Autonomous Trade Desk — 24/7 Production Deployment Guide

This guide covers deploying **Xira** to a 24/7 Cloud VPS (DigitalOcean, AWS EC2, Hetzner, Vultr, or Linode) running Ubuntu 22.04 / 24.04 LTS.

---

## ⚡ Option 1: Docker Compose Deployment (Recommended)

Docker guarantees consistent runtime isolation, automated container restart, log rotation, and persistent volume mounts.

### 1. Install Docker & Compose on your VPS
```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
```

### 2. Clone or Copy the Repository
```bash
git clone <your-repo-url> /opt/xira
cd /opt/xira
```

### 3. Configure Your Environment (`.env`)
Create or edit your `.env` file with your credentials:
```bash
cp .env.example .env
nano .env
```
Ensure the following keys are set:
```ini
TRADE_MODE=demo                      # "demo" or "live"
BYBIT_DEMO_ENV=demo_uta              # "demo_uta", "testnet", or "paper"
BYBIT_DEMO_KEY=your_bybit_api_key
BYBIT_DEMO_SECRET=your_bybit_api_secret

TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### 4. Build and Start Xira
```bash
docker compose up -d --build
```

### 5. Check Logs & Dashboard
- **Follow logs in real-time**:
  ```bash
  docker compose logs -f
  ```
- **Access Web Dashboard**:
  Open `http://<your-vps-ip>:8765` in your browser.
- **Stop or restart**:
  ```bash
  docker compose restart
  docker compose down
  ```

---

## 🖥 Option 2: Native Ubuntu Systemd Service

If you prefer running directly in Python on a Linux host:

### 1. Install System Dependencies & Python 3.11
```bash
sudo apt-get update && sudo apt-get install -y python3 python3-pip python3-venv git
```

### 2. Set Up Directory & Virtual Environment
```bash
sudo mkdir -p /opt/xira
sudo chown $USER:$USER /opt/xira
cd /opt/xira

# Copy files or git clone here
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Install Systemd Service Unit
Copy `xira.service` to the systemd directory:
```bash
sudo cp xira.service /etc/systemd/system/xira.service
sudo systemctl daemon-reload
sudo systemctl enable xira
sudo systemctl start xira
```

### 4. Manage the Service
- **Check Status**: `sudo systemctl status xira`
- **View Live Logs**: `journalctl -u xira -f -o cat`
- **Restart Service**: `sudo systemctl restart xira`
- **Stop Service**: `sudo systemctl stop xira`

---

## 📱 Interactive Telegram Bot (`@xirawinsbot`)

Once started, Xira listens for interactive commands from your authorized Telegram chat:

| Command | Action |
|---|---|
| `/status` | Live wallet equity, available USDT, active position count, and win/loss record |
| `/positions` | Real-time active Bybit positions, entry price, mark price, floating PnL, TP1, TP2, SL |
| `/scan` | Triggers an immediate market scalp scan across Core 24 and Extras |
| `/pause` | Safely pauses order execution (resting orders/positions remain protected) |
| `/resume` | Resumes automated scanning and execution |
| `/close <TICKER>` | Immediately closes open Bybit position for given ticker (e.g. `/close HYPE`) |
| `/closeall` | 🚨 Emergency panic button: market closes all open Bybit positions |
| `/help` | Displays the help menu with all available commands |

---

## 🛡 Security & Production Checklist

1. **Firewall / UFW**:
   If hosting the dashboard publicly, restrict port 8765 to your IP or use an SSH tunnel:
   ```bash
   sudo ufw allow 22/tcp
   sudo ufw allow from <YOUR_HOME_IP> to any port 8765 proto tcp
   sudo ufw enable
   ```
   Or access locally via SSH port forwarding:
   ```bash
   ssh -L 8765:localhost:8765 user@<vps-ip>
   # Then visit http://localhost:8765 on your local machine
   ```
2. **Bybit API Key Security**:
   - For live trading, always bind the API key to your VPS's static IPv4 address in Bybit API Management.
   - Restrict permissions to Unified Trading Account (Read/Write Orders & Positions only). Never enable Withdrawal permissions.
3. **State Persistence**:
   - `state.json` persists trade history, win-loss statistics, and local execution states across restarts.
