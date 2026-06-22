#!/bin/bash

# Frontend Deployment Script for EC2
# This script helps deploy your frontend to EC2

set -e  # Exit on error

# Configuration
EC2_IP="13.233.151.151"
EC2_USER="ubuntu"
KEY_PATH="$HOME/.ssh/autoscaling-backend-key.pem"
PROJECT_ROOT="/Users/ritesh/Desktop/CCfinal"

echo "🚀 Starting Frontend Deployment to EC2..."
echo "Target: $EC2_IP"

# Step 1: Build Frontend
echo ""
echo "📦 Step 1: Building Frontend..."
cd "$PROJECT_ROOT/frontend"

# Update .env
echo "REACT_APP_BACKEND_URL=http://13.233.151.151:8000" > .env

# Install dependencies
if [ ! -d "node_modules" ]; then
    echo "Installing dependencies..."
    yarn install || npm install
fi

# Build
echo "Building..."
yarn build || npm run build

echo "✅ Build complete!"

# Step 2: Upload to EC2
echo ""
echo "📤 Step 2: Uploading to EC2..."

# Create frontend directory on EC2 (if not exists)
ssh -i "$KEY_PATH" "$EC2_USER@$EC2_IP" "mkdir -p ~/frontend"

# Upload build folder
echo "Uploading build folder..."
scp -i "$KEY_PATH" -r build "$EC2_USER@$EC2_IP":~/frontend/

# Upload package files
echo "Uploading package files..."
scp -i "$KEY_PATH" package.json "$EC2_USER@$EC2_IP":~/frontend/
scp -i "$KEY_PATH" package-lock.json "$EC2_USER@$EC2_IP":~/frontend/ 2>/dev/null || scp -i "$KEY_PATH" yarn.lock "$EC2_USER@$EC2_IP":~/frontend/ 2>/dev/null || true

echo "✅ Upload complete!"

# Step 3: Setup on EC2
echo ""
echo "⚙️  Step 3: Setting up on EC2..."

ssh -i "$KEY_PATH" "$EC2_USER@$EC2_IP" << 'EOF'

echo "Installing Node.js (if needed)..."
if ! command -v node &> /dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
    sudo apt install -y nodejs
    echo "✅ Node.js installed"
else
    echo "✅ Node.js already installed: $(node --version)"
fi

echo ""
echo "Installing serve globally..."
sudo npm install -g serve

echo ""
echo "Creating systemd service..."
sudo tee /etc/systemd/system/autoscaling-frontend.service > /dev/null << 'SYSTEMD_EOF'
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
SYSTEMD_EOF

echo "✅ Service file created"

echo ""
echo "Starting frontend service..."
sudo systemctl daemon-reload
sudo systemctl enable autoscaling-frontend
sudo systemctl start autoscaling-frontend

echo ""
echo "Checking service status..."
sudo systemctl status autoscaling-frontend

echo ""
echo "✅ Frontend setup complete!"

EOF

echo ""
echo "✅ Deployment complete!"
echo ""
echo "🌐 Your application is now running at:"
echo "   Frontend: http://13.233.151.151:3000"
echo "   Backend:  http://13.233.151.151:8000/api"
echo ""
echo "📊 Check logs:"
echo "   ssh -i $KEY_PATH $EC2_USER@$EC2_IP"
echo "   sudo journalctl -u autoscaling-frontend -f"
echo ""
