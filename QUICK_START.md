# Quick Start Guide

## 🚀 Fastest Way to Run the Application

### Step 1: Backend Setup (5 minutes)

```bash
# 1. Navigate to backend
cd backend

# 2. Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create .env file
cat > .env << EOF
MONGO_URL=mongodb://localhost:27017
DB_NAME=autoscaling_db
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
EOF

# 5. Start the server
python -m uvicorn server:app --reload --port 8000 --host 0.0.0.0
```

### Step 2: Frontend Setup (3 minutes)

**Open a new terminal:**

```bash
# 1. Navigate to frontend
cd frontend

# 2. Install dependencies
yarn install
# OR: npm install

# 3. Create .env file (optional, defaults to http://localhost:8000)
# For AWS deployment: echo "REACT_APP_BACKEND_URL=http://13.233.151.151:8000" > .env
echo "REACT_APP_BACKEND_URL=http://localhost:8000" > .env

# 4. Start the frontend
yarn start
# OR: npm start
```

### Step 3: Access the Application

- Frontend: http://localhost:3000
- Backend API: http://localhost:8000/api
- API Docs: http://localhost:8000/docs

## ⚠️ Important Notes

1. **MongoDB**: The backend requires MongoDB. If you don't have it installed:
   - Install MongoDB locally, OR
   - Use MongoDB Atlas (free tier): https://www.mongodb.com/cloud/atlas
   - Update `MONGO_URL` in `backend/.env`

2. **Model Files**: Ensure these files exist in the `backend` directory:
   - `catboost_hourly_model.pkl`
   - `lightgbm_hourly_model.pkl`
   - `xgboost_hourly_model.pkl`
   - `feature_columns_hourly.pkl`
   - `lstm_hourly_model_fixed.h5` (optional)

3. **Ports**:
   - Backend: 8000
   - Frontend: 3000
   - Make sure these ports are not in use

## 🧪 Test the Setup

```bash
# Test backend health
curl http://localhost:8000/api/health

# Test predictions
curl -X POST http://localhost:8000/api/predict \
  -H "Content-Type: application/json" \
  -d '{
    "start_time": "2025-01-20T00:00:00Z",
    "hours": 24,
    "model_name": "catboost"
  }'
```

## 🐛 Troubleshooting

### Backend won't start
- Check if MongoDB is running: `mongosh` or check MongoDB service
- Verify Python version: `python --version` (need 3.9+)
- Check if port 8000 is free: `lsof -i :8000`

### Frontend won't connect to backend
- Verify backend is running on port 8000
- Check `REACT_APP_BACKEND_URL` in `frontend/.env`
- Check browser console for CORS errors

### Models not loading
- Verify all `.pkl` and `.h5` files are in `backend/` directory
- Check backend logs for model loading errors

## 📚 For More Details

See [README.md](./README.md) for complete documentation.

