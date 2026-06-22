# 🎯 Quick Frontend Deployment Steps (5 Minutes)

Your backend is already running on `13.233.151.151:8000`. Now let's deploy the frontend!

## ⚡ Fastest Way (Copy-Paste Commands)

### Step 1: Build Frontend (On Your Local Machine)

```bash
cd ~/Desktop/CCfinal/frontend

# Update backend URL
echo "REACT_APP_BACKEND_URL=http://13.233.151.151:8000" > .env

# Build
yarn build
# OR: npm run build

# Verify build created
ls -la build/ | head -10
```

### Step 2: Upload Build to EC2

```bash
# From ~/Desktop/CCfinal directory
cd ~/Desktop/CCfinal

scp -i ~/.ssh/autoscaling-backend-key.pem -r frontend/build ubuntu@13.233.151.151:~/frontend/
scp -i ~/.ssh/autoscaling-backend-key.pem frontend/package*.json ubuntu@13.233.151.151:~/frontend/
```

### Step 3: SSH into EC2 and Setup

```bash
ssh -i ~/.ssh/autoscaling-backend-key.pem ubuntu@13.233.151.151
```

Once you're in EC2, run these commands:

```bash
# Install Node.js
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install -y nodejs

# Install serve
sudo npm install -g serve

# Create systemd service
sudo tee /etc/systemd/system/autoscaling-frontend.service > /dev/null << 'EOF'
[Unit]
Description=AI Predictive Autoscaling Frontend
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/frontend
ExecStart=/usr/bin/serve -s build -l 3000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Start frontend
sudo systemctl daemon-reload
sudo systemctl enable autoscaling-frontend
sudo systemctl start autoscaling-frontend

# Check status
sudo systemctl status autoscaling-frontend
```

### Step 4: Test

```bash
# In your local machine terminal
curl http://13.233.151.151:3000

# Open browser: http://13.233.151.151:3000
```

---

## 🔧 Useful Commands After Deployment

### Check Status

```bash
# SSH into EC2
ssh -i ~/.ssh/autoscaling-backend-key.pem ubuntu@13.233.151.151

# Check both services
sudo systemctl status autoscaling-backend
sudo systemctl status autoscaling-frontend

# Both should show "active (running)" in green
```

### View Logs

```bash
# Real-time logs for frontend
sudo journalctl -u autoscaling-frontend -f

# Real-time logs for backend
sudo journalctl -u autoscaling-backend -f

# Press Ctrl+C to exit logs
```

### Restart Services

```bash
# If frontend needs restart
sudo systemctl restart autoscaling-frontend

# If backend needs restart
sudo systemctl restart autoscaling-backend

# Restart all
sudo systemctl restart autoscaling-frontend autoscaling-backend
```

### Update Frontend Code

```bash
# On local machine, rebuild and upload
cd ~/Desktop/CCfinal/frontend
yarn build
scp -i ~/.ssh/autoscaling-backend-key.pem -r build ubuntu@13.233.151.151:~/frontend/

# Then restart service on EC2
ssh -i ~/.ssh/autoscaling-backend-key.pem ubuntu@13.233.151.151 "sudo systemctl restart autoscaling-frontend"
```

---

## ✅ Deployment Checklist

- [ ] Frontend `.env` updated to `http://13.233.151.151:8000`
- [ ] Frontend built with `yarn build`
- [ ] Build folder uploaded to EC2
- [ ] Node.js installed on EC2
- [ ] `serve` package installed globally
- [ ] Systemd service created
- [ ] Frontend service started and running
- [ ] Both services accessible:
  - [ ] `http://13.233.151.151:3000` (Frontend)
  - [ ] `http://13.233.151.151:8000/api/health` (Backend)

---

## 📊 Architecture After Deployment

```
EC2 Instance (13.233.151.151) - Mumbai Region
│
├── Port 3000: Frontend (React)
│   └── Serves via: serve command
│   └── Auto-restart: Yes (systemd)
│
├── Port 8000: Backend (FastAPI)
│   └── Serves via: Uvicorn
│   └── Auto-restart: Yes (systemd)
│
└── Port 22: SSH Access
    └── For management
```

---

## 💡 Pro Tips

1. **Use `serve` instead of `npm start`** - Better for production
2. **Systemd service auto-restarts** - If app crashes, it restarts automatically
3. **Services auto-start on reboot** - You don't need to manually restart
4. **Check logs if things break** - `sudo journalctl -u autoscaling-frontend -n 50`
5. **Keep security groups updated** - Ensure ports 80, 443, 3000, 8000 are open

---

## 🚨 Troubleshooting

### "Cannot find build folder"
```bash
# Go back to local machine and build
cd ~/Desktop/CCfinal/frontend
yarn build

# Check build was created
ls -la build/

# Re-upload
scp -i ~/.ssh/autoscaling-backend-key.pem -r build ubuntu@13.233.151.151:~/frontend/
```

### Frontend shows "Cannot connect to backend"
```bash
# Check backend is running
curl http://13.233.151.151:8000/api/health

# Check CORS in backend .env
# Should include: CORS_ORIGINS=http://13.233.151.151:3000

# Restart backend
ssh -i ~/.ssh/autoscaling-backend-key.pem ubuntu@13.233.151.151
sudo systemctl restart autoscaling-backend
```

### Port 3000 already in use
```bash
# Find what's using port 3000
sudo lsof -i :3000

# Kill it (replace PID with actual number)
sudo kill -9 <PID>

# Restart service
sudo systemctl restart autoscaling-frontend
```

---

## 📞 Support

If you need help:

1. Check logs: `sudo journalctl -u autoscaling-frontend -n 100`
2. Verify services are running: `sudo systemctl status autoscaling-*`
3. Test connectivity: `curl http://13.233.151.151:3000`
4. Review security groups in AWS console
5. Check EC2 instance is in "Running" state

---

**Last Updated**: November 2025
**Time to Deploy**: ~10 minutes
