# 🚀 Deploy Updated Backend to EC2

Your backend code is now updated to use IAM roles and has scaling safeguards. Follow these steps to deploy:

---

## Step 1: Upload Updated Code to EC2

From your **local machine**:

```bash
cd ~/Desktop/CCfinal

# Upload backend code
scp -i ~/.ssh/autoscaling-backend-key.pem -r backend/ ubuntu@13.233.151.151:~/CCfinal/

# Or if you're in a git repo, just push and pull on EC2
git add .
git commit -m "Update: IAM role support, scaling safeguards, correct ASG name"
git push origin main
```

---

## Step 2: SSH into EC2

```bash
ssh -i ~/.ssh/autoscaling-backend-key.pem ubuntu@13.233.151.151
```

---

## Step 3: Navigate to Backend

```bash
cd ~/CCfinal/backend

# If you used git pull:
cd ~/CCfinal
git pull origin main
cd backend
```

---

## Step 4: Verify `.env` File

Check your `.env` file has the correct ASG name:

```bash
cat .env
```

Should show:
```
AWS_REGION=ap-south-1
AWS_ASG_NAME=CCfinal-ASG
HARD_MAX_CAP=2
```

---

## Step 5: Restart Backend Service

```bash
sudo systemctl restart autoscaling-backend

# Check status
sudo systemctl status autoscaling-backend

# Should show: active (running)
```

---

## Step 6: View Logs

```bash
sudo journalctl -u autoscaling-backend -f

# Look for:
# - "Application startup complete" → Backend started
# - "[MOCK] AWS autoscaling client not available" → No IAM role (problem!)
# - No error messages about credentials → IAM role is working!

# Press Ctrl+C to exit logs
```

---

## Step 7: Test Scaling

From your **local machine**, trigger a scale request:

```bash
curl -X POST http://13.233.151.151:8000/api/scale \
  -H "Content-Type: application/json" \
  -d '{"predicted_load":2500}'
```

**Expected response:**
```json
{
  "success": true,
  "mode": "real",
  "predicted_load": 2500,
  "recommended_capacity": 4,
  "current_desired": 1,
  "desired_capacity": 2,
  "message": "Scaled CCfinal-ASG to 2 instances"
}
```

---

## Step 8: Verify on AWS Console

1. Go to AWS Console → EC2 → Auto Scaling Groups
2. Select `CCfinal-ASG`
3. Check "Activity" tab
4. Should see a scaling event like: "Launching 1 instance" or "Terminating 1 instance"
5. Check "Instances" tab → only 1-2 instances should be running

---

## 🎯 Key Points

✅ **IAM Role** - Backend uses `EC2-AutoScalingRole` (no keys needed)  
✅ **ASG Name** - Now reads `AWS_ASG_NAME=CCfinal-ASG` from `.env`  
✅ **Safeguards** - Max 2 instances, 5-minute cooldown, safe scale logic  
✅ **No Errors** - Should not see "my-asg not found" anymore  

---

## ❌ Troubleshooting

**Error: "ASG CCfinal-ASG not found"**
- Check AWS Console → verify ASG name is exactly `CCfinal-ASG`
- Verify IAM role has permission to describe ASGs
- Check region is correct: `AWS_REGION=ap-south-1`

**Error: "AWS autoscaling client not available"**
- Check IAM role is attached to your EC2 instance
- Restart backend: `sudo systemctl restart autoscaling-backend`

**Scaling not happening**
- Check cooldown: `SCALE_COOLDOWN_SECONDS=300` (5 minutes between requests)
- Check hard cap: `HARD_MAX_CAP=2` (won't scale beyond 2)
- View logs: `sudo journalctl -u autoscaling-backend -f`

---

**Done! Your backend is now running with IAM roles and safe scaling limits. 🚀**
