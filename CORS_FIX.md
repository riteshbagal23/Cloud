# 🔧 CORS Fix - Complete Guide

## What Was the Problem?

Your frontend was trying to access the backend API, but the backend's CORS (Cross-Origin Resource Sharing) policy didn't allow requests from `http://13.233.151.151`.

**Error Message:**
```
Access to XMLHttpRequest at 'http://13.233.151.151:8000/api/predict' from origin 
'http://13.233.151.151' has been blocked by CORS policy: No 'Access-Control-Allow-Origin' 
header is present on the requested resource.
```

---

## What I Fixed

### 1. Updated `backend/server.py`

Added `http://13.233.151.151` and its variants to the allowed origins:

```python
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', 
        'http://localhost:3000,'
        'http://127.0.0.1:3000,'
        'http://13.233.151.151,'              # Frontend on main domain
        'http://13.233.151.151:3000,'         # Frontend on port 3000
        'http://13.233.151.151:8000,'         # Backend on port 8000
        'http://ec2-15-207-98-239.ap-south-1.compute.amazonaws.com,'
        'http://15.207.98.239.com'
    ).split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### 2. Created `backend/.env`

```env
MONGO_URL=mongodb://localhost:27017
DB_NAME=autoscaling_db
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000,http://13.233.151.151,http://13.233.151.151:3000,http://13.233.151.151:8000
```

---

## What You Need to Do Now

### Step 1: Restart Your Backend

Your backend needs to reload the new CORS settings.

**On your EC2 instance (13.233.151.151):**

```bash
ssh -i ~/.ssh/autoscaling-backend-key.pem ubuntu@13.233.151.151

# Restart the backend service
sudo systemctl restart autoscaling-backend

# Check status
sudo systemctl status autoscaling-backend

# View logs
sudo journalctl -u autoscaling-backend -f
```

### Step 2: Refresh Frontend

- Open your browser
- Go to `http://13.233.151.151:3000`
- **Hard refresh** (Ctrl+Shift+R on Windows/Linux, Cmd+Shift+R on Mac)
- Clear browser cache if needed

### Step 3: Test

Try making a prediction. The CORS error should be gone!

---

## Why This Happens

### What is CORS?

CORS (Cross-Origin Resource Sharing) is a security feature that prevents malicious websites from accessing your API.

**Example:**
- ❌ `http://malicious-site.com` trying to access `http://13.233.151.151:8000` → **BLOCKED**
- ✅ `http://13.233.151.151` trying to access `http://13.233.151.151:8000` → **ALLOWED**

### Why You Needed the Fix

Your setup has:
- **Frontend**: `http://13.233.151.151:3000` (running on serve)
- **Backend**: `http://13.233.151.151:8000` (running on FastAPI)

When the frontend tried to call the backend, it was coming from origin `http://13.233.151.151:3000`, which wasn't in the allowed list.

---

## How CORS Works

1. **Browser sees cross-origin request** (different domain/port)
2. **Browser sends OPTIONS preflight request** to check if allowed
3. **Backend responds** with `Access-Control-Allow-Origin` header
4. **If allowed**, browser allows the actual request
5. **If not allowed**, browser blocks it (shows CORS error)

---

## Current Allowed Origins

Your backend now allows requests from:

| Origin | Purpose |
|--------|---------|
| `http://localhost:3000` | Local development |
| `http://127.0.0.1:3000` | Local development (IP) |
| `http://13.233.151.151` | Frontend on EC2 (port 80) |
| `http://13.233.151.151:3000` | Frontend on EC2 (port 3000) |
| `http://13.233.151.151:8000` | Backend on EC2 (for testing) |
| `http://ec2-15-207-98-239.ap-south-1.compute.amazonaws.com` | Old EC2 domain |
| `http://15.207.98.239.com` | Old domain |

---

## If You Still Get CORS Errors

### Check 1: Backend is Running

```bash
ssh -i ~/.ssh/autoscaling-backend-key.pem ubuntu@13.233.151.151
sudo systemctl status autoscaling-backend
```

Should show: `active (running)`

### Check 2: Backend Restarted After Changes

```bash
ssh -i ~/.ssh/autoscaling-backend-key.pem ubuntu@13.233.151.151
sudo systemctl restart autoscaling-backend
sleep 2
curl http://13.233.151.151:8000/api/health
```

### Check 3: Frontend Refreshed Cache

- **Hard refresh**: Ctrl+Shift+R (Windows/Linux) or Cmd+Shift+R (Mac)
- **Or clear cache**: DevTools → Application → Cache → Clear

### Check 4: Verify CORS Headers

```bash
# This should show CORS headers
curl -i -X OPTIONS http://13.233.151.151:8000/api/predict \
  -H "Origin: http://13.233.151.151:3000" \
  -H "Access-Control-Request-Method: POST"

# Look for: "access-control-allow-origin: http://13.233.151.151:3000"
```

### Check 5: Check Browser Console

- Open DevTools (F12)
- Go to **Console** tab
- Look for the exact error message
- Check **Network** tab to see the OPTIONS request response

---

## Common CORS Errors Explained

### Error 1: "No 'Access-Control-Allow-Origin' header"
**Cause:** Origin not in allowed list
**Fix:** Add origin to CORS_ORIGINS in `.env`

### Error 2: "Response to preflight request doesn't pass access control check"
**Cause:** Method or headers not allowed
**Fix:** Verify `allow_methods=["*"]` and `allow_headers=["*"]` in code

### Error 3: "Credentials mode is 'include' but Access-Control-Allow-Credentials is missing"
**Cause:** Credentials needed but not enabled
**Fix:** Ensure `allow_credentials=True` in middleware

---

## Files Changed

✅ `/Users/ritesh/Desktop/CCfinal/backend/server.py` - Updated CORS origins  
✅ `/Users/ritesh/Desktop/CCfinal/backend/.env` - Created with CORS_ORIGINS

---

## Next Steps

1. ✅ Restart backend on EC2
2. ✅ Hard refresh frontend
3. ✅ Test predictions
4. ✅ Check browser console for errors
5. ⏭️ Deploy to production

---

## Additional Resources

- [CORS Documentation](https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS)
- [FastAPI CORS Documentation](https://fastapi.tiangolo.com/tutorial/cors/)
- [Browser CORS Debugging](https://www.html5rocks.com/en/tutorials/cors/)

---

**Last Updated**: November 2025
