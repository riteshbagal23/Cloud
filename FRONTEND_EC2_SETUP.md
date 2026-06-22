# 🚀 Frontend Deployment on EC2 (Same Instance as Backend)

This guide shows how to host your React frontend on the same EC2 instance (13.233.151.151) as your backend.

## Architecture

```
EC2 Instance (13.233.151.151)
├── Backend (Port 8000) - FastAPI/Uvicorn
├── Frontend (Port 3000) - React/npm
└── Nginx (Port 80/443) - Reverse Proxy
```

## Option 1: Run Frontend with npm (Development Mode)

### Step 1: Build Frontend on Local Machine

```bash
cd frontend

# Update .env to point to your backend IP
echo "REACT_APP_BACKEND_URL=http://13.233.151.151:8000" > .env

# Install dependencies
yarn install

# Build for production
yarn build

# Verify build folder exists
ls -la build/
```

### Step 2: Upload Frontend Build to EC2

From your local machine:

```bash
# Navigate to your project root
cd /path/to/CCfinal

# Upload frontend build folder to EC2
scp -i ~/.ssh/autoscaling-backend-key.pem -r frontend/build ubuntu@13.233.151.151:~/

# Also upload package.json and other files
scp -i ~/.ssh/autoscaling-backend-key.pem frontend/package.json ubuntu@13.233.151.151:~/frontend/
scp -i ~/.ssh/autoscaling-backend-key.pem frontend/package-lock.json ubuntu@13.233.151.151:~/frontend/
scp -i ~/.ssh/autoscaling-backend-key.pem -r frontend/public ubuntu@13.233.151.151:~/frontend/
```

### Step 3: Connect to EC2 and Set Up Frontend

```bash
# SSH into EC2
ssh -i ~/.ssh/autoscaling-backend-key.pem ubuntu@13.233.151.151

# Navigate to frontend directory
cd ~/frontend

# Install dependencies
npm install
# OR
yarn install

# Verify build folder exists
ls -la build/
```

### Step 4: Install Node.js (if not already installed)

```bash
# Install Node.js 18.x
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install -y nodejs

# Verify installation
node --version
npm --version
```

### Step 5: Run Frontend on Port 3000

```bash
# Option A: Run in development mode
npm start

# Option B: Serve the build folder (production)
npx serve -s build -l 3000
```

---

## Option 2: Run Frontend with Serve (Production - Recommended)

This is better for production as it serves the build folder without development overhead.

### Step 1: Install Serve Globally

On EC2:

```bash
# Install serve globally
sudo npm install -g serve

# Verify
serve --version
```

### Step 2: Run Frontend

```bash
# Navigate to frontend directory
cd ~/frontend

# Serve the build folder on port 3000
serve -s build -l 3000

# You should see: "Accepting connections at http://localhost:3000"
```

### Step 3: Test from Your Local Machine

```bash
# Test if frontend is accessible
curl http://13.233.151.151:3000

# Should return HTML content
```

---

## Option 3: Run Frontend as a Systemd Service (Auto-Start - Recommended)

This ensures your frontend auto-starts when the server reboots.

### Step 1: Create Systemd Service File

On EC2:

```bash
# Create service file
sudo nano /etc/systemd/system/autoscaling-frontend.service
```

Paste this content:

```ini
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
Environment="PATH=/usr/local/bin:/usr/bin"

[Install]
WantedBy=multi-user.target
```

Save (Ctrl+X, Y, Enter)

### Step 2: Enable and Start Service

```bash
# Reload systemd
sudo systemctl daemon-reload

# Enable service to start on boot
sudo systemctl enable autoscaling-frontend

# Start the service
sudo systemctl start autoscaling-frontend

# Check status
sudo systemctl status autoscaling-frontend

# View logs
sudo journalctl -u autoscaling-frontend -f
```

---

## Option 4: Configure Nginx to Serve Frontend (Recommended for Production)

Nginx can serve both backend API and frontend from different ports/paths.

### Step 1: Verify Nginx is Installed

```bash
# Check if nginx is running
sudo systemctl status nginx

# If not installed:
sudo apt install nginx -y
```

### Step 2: Update Nginx Configuration

```bash
# Edit nginx config
sudo nano /etc/nginx/sites-available/autoscaling-backend
```

Replace with this (handles both backend and frontend):

```nginx
server {
    listen 80 default_server;
    server_name 13.233.151.151;

    # Serve frontend from root path
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }

    # Serve backend API from /api path
    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }

    # Serve backend docs from /docs
    location /docs {
        proxy_pass http://127.0.0.1:8000/docs;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Save (Ctrl+X, Y, Enter)

### Step 3: Test and Restart Nginx

```bash
# Test nginx configuration
sudo nginx -t

# Should see: "test is successful"

# Restart nginx
sudo systemctl restart nginx

# Check status
sudo systemctl status nginx
```

### Step 4: Update Frontend .env

```bash
# In frontend/.env, update backend URL to use same domain
echo "REACT_APP_BACKEND_URL=http://13.233.151.151" > .env

# Or if running through nginx:
echo "REACT_APP_BACKEND_URL=http://13.233.151.151/api" > .env
```

---

## Security Group Configuration

Make sure your EC2 security group allows these ports:

1. **Port 80 (HTTP)** - For Nginx/Frontend
   - Type: HTTP
   - Source: 0.0.0.0/0 (Anywhere)

2. **Port 443 (HTTPS)** - For Nginx/Frontend (if using SSL)
   - Type: HTTPS
   - Source: 0.0.0.0/0

3. **Port 3000 (Frontend)** - Direct access
   - Type: Custom TCP, Port 3000
   - Source: 0.0.0.0/0 (optional if using Nginx)

4. **Port 8000 (Backend)** - API access
   - Type: Custom TCP, Port 8000
   - Source: 0.0.0.0/0 (optional if using Nginx)

---

## Quick Start Guide - Recommended Setup

Here's the fastest way to get everything running:

### On Your Local Machine

```bash
# 1. Build frontend
cd frontend
echo "REACT_APP_BACKEND_URL=http://13.233.151.151:8000" > .env
yarn build

# 2. Upload build to EC2
cd ../
scp -i ~/.ssh/autoscaling-backend-key.pem -r frontend/build ubuntu@13.233.151.151:~/frontend/
scp -i ~/.ssh/autoscaling-backend-key.pem frontend/package*.json ubuntu@13.233.151.151:~/frontend/
```

### On EC2 Instance

```bash
# 1. SSH into instance
ssh -i ~/.ssh/autoscaling-backend-key.pem ubuntu@13.233.151.151

# 2. Install Node.js (if not installed)
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install -y nodejs

# 3. Install serve
sudo npm install -g serve

# 4. Create systemd service for frontend
sudo tee /etc/systemd/system/autoscaling-frontend.service > /dev/null << EOF
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

# 5. Enable and start frontend service
sudo systemctl daemon-reload
sudo systemctl enable autoscaling-frontend
sudo systemctl start autoscaling-frontend

# 6. Check status
sudo systemctl status autoscaling-frontend

# 7. Verify both backend and frontend are running
curl http://127.0.0.1:8000/api/health    # Backend
curl http://127.0.0.1:3000               # Frontend
```

---

## Test Your Deployment

### From Local Machine

```bash
# Test Frontend
curl http://13.233.151.151:3000

# Test Backend API
curl http://13.233.151.151:8000/api/health

# Test Frontend in Browser
# Open: http://13.233.151.151:3000
```

### Check Services

```bash
# SSH into EC2
ssh -i ~/.ssh/autoscaling-backend-key.pem ubuntu@13.233.151.151

# Check both services are running
sudo systemctl status autoscaling-backend
sudo systemctl status autoscaling-frontend

# View logs
sudo journalctl -u autoscaling-frontend -f
sudo journalctl -u autoscaling-backend -f
```

---

## Troubleshooting

### Frontend Not Loading

```bash
# Check if service is running
sudo systemctl status autoscaling-frontend

# Check logs
sudo journalctl -u autoscaling-frontend -n 50

# Check if port 3000 is listening
sudo netstat -tlnp | grep 3000

# Restart service
sudo systemctl restart autoscaling-frontend
```

### Backend API Not Responding from Frontend

1. **Check CORS Settings** in backend `.env`:
   ```bash
   CORS_ORIGINS=http://13.233.151.151:3000,http://13.233.151.151:8000
   ```

2. **Update Frontend URL** in `frontend/.env`:
   ```bash
   REACT_APP_BACKEND_URL=http://13.233.151.151:8000
   ```

3. **Rebuild Frontend**:
   ```bash
   cd frontend
   npm run build
   # Then reupload build/ folder to EC2
   ```

### Port Already in Use

```bash
# Find what's using port 3000
sudo lsof -i :3000

# Find what's using port 8000
sudo lsof -i :8000

# Kill process if needed
sudo kill -9 <PID>
```

### Can't SSH into EC2

```bash
# Check key permissions
chmod 400 ~/.ssh/autoscaling-backend-key.pem

# Verify public IP is correct
# AWS Console → EC2 → Instances → Check "Public IPv4 address"

# Check security group allows SSH (port 22)
# AWS Console → EC2 → Security Groups → Inbound Rules
```

---

## Directory Structure on EC2

After setup, your EC2 instance should have:

```
/home/ubuntu/
├── backend/
│   ├── .venv/
│   ├── server.py
│   ├── .env
│   ├── requirements.txt
│   ├── *.pkl (model files)
│   └── ...
├── frontend/
│   ├── build/
│   ├── public/
│   ├── src/
│   ├── package.json
│   ├── package-lock.json
│   └── .env
└── global-bundle.pem (if using DocumentDB)
```

---

## Estimated Costs (per month)

| Component | Cost |
|-----------|------|
| EC2 t3.medium (1 instance) | ~$30 |
| Data transfer | ~$10-20 |
| MongoDB Atlas (free tier) | FREE |
| **Total** | **~$40-50** |

---

## Next Steps

1. ✅ Deploy backend on EC2 port 8000
2. ✅ Deploy frontend on EC2 port 3000
3. ⏭️ Set up SSL certificate (Let's Encrypt)
4. ⏭️ Configure domain name (optional)
5. ⏭️ Set up monitoring and alerts
6. ⏭️ Set up auto-scaling (if needed)

---

## Useful Commands

```bash
# Check all running services
sudo systemctl list-units --type=service --state=running

# Restart all services
sudo systemctl restart autoscaling-backend
sudo systemctl restart autoscaling-frontend
sudo systemctl restart nginx

# View real-time logs
sudo journalctl -u autoscaling-backend -f
sudo journalctl -u autoscaling-frontend -f

# Check disk usage
df -h

# Check memory usage
free -h

# Monitor services in real-time
watch 'sudo systemctl status autoscaling-backend; echo "---"; sudo systemctl status autoscaling-frontend'
```

---

**Last Updated**: November 2025
