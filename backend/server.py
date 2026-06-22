from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response

# Prometheus metrics
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
import os
import logging
import warnings
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Dict, Any
import uuid
from datetime import datetime, timezone, timedelta
import joblib
import pandas as pd
import numpy as np
import requests
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# MongoDB connection (optional - graceful handling)
# Try to import Motor, but don't fail if it's not available or has compatibility issues
mongo_url = os.environ.get('MONGO_URL', '')
db_name = os.environ.get('DB_NAME', 'autoscaling_db')
client = None
db = None
MOTOR_AVAILABLE = False

try:
    from motor.motor_asyncio import AsyncIOMotorClient
    MOTOR_AVAILABLE = True
    
    if mongo_url:
        try:
            client = AsyncIOMotorClient(mongo_url)
            db = client[db_name]
            logger.info("✅ MongoDB connected successfully")
        except Exception as e:
            logger.warning(f"⚠️ MongoDB connection failed: {e}. Status checks will be disabled.")
            client = None
            db = None
    else:
        logger.info("ℹ️ MONGO_URL not set. Status checks will be disabled.")
except ImportError as e:
    logger.warning(f"⚠️ Motor (MongoDB driver) not available: {e}. Status checks will be disabled.")
    logger.info("ℹ️ To enable MongoDB features, install compatible versions: pip install 'motor>=3.3.0' 'pymongo>=4.0,<5.0'")
except Exception as e:
    logger.warning(f"⚠️ Error loading Motor: {e}. Status checks will be disabled.")
    logger.info("ℹ️ MongoDB features are optional. The server will continue without them.")

# Create the main app without a prefix
app = FastAPI(title="AI Predictive Autoscaling System")

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# --------------------- Prometheus metrics ---------------------
# Counters and histograms for basic HTTP metrics and predictions
HTTP_REQUESTS_TOTAL = Counter(
    'http_requests_total', 'Total HTTP requests', ['method', 'endpoint', 'http_status']
)
HTTP_REQUEST_LATENCY_SECONDS = Histogram(
    'http_request_latency_seconds', 'HTTP request latency in seconds', ['endpoint']
)


@app.middleware("http")
async def prometheus_middleware(request, call_next):
    """Middleware to track request counts and latency for Prometheus."""
    path = request.url.path
    method = request.method
    with HTTP_REQUEST_LATENCY_SECONDS.labels(endpoint=path).time():
        try:
            response = await call_next(request)
            status_code = getattr(response, 'status_code', 200)
        except Exception as e:
            # On exceptions, increment counter with 500
            HTTP_REQUESTS_TOTAL.labels(method=method, endpoint=path, http_status='500').inc()
            raise

    HTTP_REQUESTS_TOTAL.labels(method=method, endpoint=path, http_status=str(status_code)).inc()
    return response


@app.get("/metrics")
def metrics():
    """Expose Prometheus metrics."""
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)

# ------------------- end Prometheus metrics -------------------

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# HELPER FUNCTIONS & CACHING
# ============================================================================

# Simple in-memory cache for predictions (TTL: 30 minutes for better performance)
PREDICTION_CACHE = {}
CACHE_TTL = 1800  # 30 minutes - longer cache for better performance
LAST_CACHE_CLEANUP = 0  # Track last cleanup time
CACHE_CLEANUP_INTERVAL = 300  # Clean cache every 5 minutes (not on every request)

# In-memory small state to track last scale time per ASG to enforce cooldowns.
# This is intentionally simple; for multiple-process deployments consider a
# shared store (Redis/DB) to coordinate cooldown across instances.
SCALE_STATE = {}

# Cache for calendar/festival API calls (TTL: 24 hours - festivals don't change)
FESTIVAL_CACHE = {}
FESTIVAL_CACHE_TTL = 86400  # 24 hours

# Track model reliability (to avoid repeated failures)
MODEL_FAILURE_COUNT = {}
MAX_FAILURES = 3  # After 3 failures, skip the model for this session

# Fixed festival name mapping for XGBoost (matches training data encoding)
# This ensures consistent encoding - 'None' should be 0, not alphabetically sorted
XGBOOST_FESTIVAL_MAP = {
    'None': 0,
    'Diwali': 1,
    'Holi': 2,
    'Christmas': 3,
    'Independence Day': 4,
    'Republic Day': 5,
    'Ram Navami': 6,
    'Diwali Weekend': 7
}

def encode_festival_name_for_xgboost(festival_name: str) -> int:
    """Encode festival name to numeric value for XGBoost model"""
    return XGBOOST_FESTIVAL_MAP.get(str(festival_name), 0)

def parse_iso_datetime(dt_str: str) -> datetime:
    """Parse ISO format datetime string, handling 'Z' suffix for UTC"""
    # Replace 'Z' with '+00:00' for UTC timezone (Python 3.9 compatibility)
    if dt_str.endswith('Z'):
        dt_str = dt_str.replace('Z', '+00:00')
    return datetime.fromisoformat(dt_str)

def get_cache_key(timestamp: datetime, model_name: str) -> str:
    """Generate cache key for prediction"""
    return f"{timestamp.strftime('%Y-%m-%d-%H')}-{model_name}"

def clear_old_cache():
    """Clear cache entries older than TTL - optimized to run less frequently"""
    global LAST_CACHE_CLEANUP
    current_time = datetime.now(timezone.utc).timestamp()
    
    # Only cleanup every 5 minutes (not on every request)
    if current_time - LAST_CACHE_CLEANUP < CACHE_CLEANUP_INTERVAL:
        return
    
    LAST_CACHE_CLEANUP = current_time
    
    # Efficient cleanup
    keys_to_remove = [k for k, (_, cached_time) in PREDICTION_CACHE.items() if current_time - cached_time > CACHE_TTL]
    for k in keys_to_remove:
        del PREDICTION_CACHE[k]

# ============================================================================
# LOAD ML MODELS
# ============================================================================

MODELS = {}
FEATURE_COLUMNS = None

try:
    # Model files in backend directory
    import pickle
    catboost_path = ROOT_DIR / 'catboost_hourly_model.pkl'
    lightgbm_path = ROOT_DIR / 'lightgbm_hourly_model.pkl'
    xgboost_path = ROOT_DIR / 'xgboost_hourly_model.pkl'
    # Try fixed LSTM model first, fallback to original
    lstm_path_fixed = ROOT_DIR / 'lstm_hourly_model_fixed.h5'
    lstm_path = ROOT_DIR / 'lstm_hourly_model.h5'
    if lstm_path_fixed.exists():
        lstm_path = lstm_path_fixed
        logger.info("Using fixed LSTM model (lstm_hourly_model_fixed.h5)")
    feature_cols_path = ROOT_DIR / 'feature_columns_hourly.pkl'
    
    # Load models with encoding for compatibility
    with open(catboost_path, 'rb') as f:
        MODELS['catboost'] = pickle.load(f, encoding='latin1')
    
    MODELS['lightgbm'] = joblib.load(lightgbm_path)
    
    # Suppress XGBoost warnings about serialized models
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', category=UserWarning, module='xgboost')
        MODELS['xgboost'] = joblib.load(xgboost_path)
    
    # Load LSTM model if available
    try:
        import tensorflow as tf
        if lstm_path.exists():
            # Try multiple loading strategies for compatibility
            lstm_loaded = False
            
            # Strategy 1: Standard load with compile=False
            try:
                MODELS['lstm'] = tf.keras.models.load_model(lstm_path, compile=False)
                logger.info(f"✅ Loaded LSTM model successfully (standard method)")
                lstm_loaded = True
            except Exception as e1:
                logger.debug(f"Standard LSTM load failed: {e1}")
                
                # Strategy 2: Try with safe_mode=False (for older Keras versions)
                try:
                    MODELS['lstm'] = tf.keras.models.load_model(
                        lstm_path, 
                        compile=False,
                        safe_mode=False
                    )
                    logger.info(f"✅ Loaded LSTM model successfully (safe_mode=False)")
                    lstm_loaded = True
                except Exception as e2:
                    logger.debug(f"Safe mode load failed: {e2}")
                    
                    # Strategy 3: Try loading by manually fixing the config (handle batch_shape issue)
                    try:
                        import json
                        import h5py
                        
                        # Read and fix the model config
                        with h5py.File(lstm_path, 'r') as f:
                            if 'model_config' in f:
                                config_str = f['model_config'][()].decode('utf-8')
                                config = json.loads(config_str)
                                
                                # Fix batch_shape in InputLayer configs
                                if 'config' in config and 'layers' in config['config']:
                                    for layer in config['config']['layers']:
                                        if layer.get('class_name') == 'InputLayer':
                                            layer_config = layer.get('config', {})
                                            if 'batch_shape' in layer_config:
                                                # Convert batch_shape to input_shape
                                                batch_shape = layer_config.pop('batch_shape')
                                                if batch_shape and len(batch_shape) > 1:
                                                    layer_config['input_shape'] = batch_shape[1:]
                                
                                # Save fixed config to temp file and load
                                import tempfile
                                import shutil
                                
                                with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as tmp:
                                    json.dump(config, tmp)
                                    tmp_config_path = tmp.name
                                
                                # Create a new model file with fixed config
                                with tempfile.NamedTemporaryFile(suffix='.h5', delete=False) as tmp_model:
                                    tmp_model_path = tmp_model.name
                                
                                # Copy weights and use fixed config
                                with h5py.File(lstm_path, 'r') as src, h5py.File(tmp_model_path, 'w') as dst:
                                    # Copy model_weights
                                    if 'model_weights' in src:
                                        src.copy('model_weights', dst, 'model_weights')
                                    
                                    # Write fixed config
                                    dst.create_dataset('model_config', data=json.dumps(config).encode('utf-8'))
                                
                                # Try loading the fixed model
                                MODELS['lstm'] = tf.keras.models.load_model(tmp_model_path, compile=False)
                                
                                # Cleanup
                                import os
                                os.unlink(tmp_config_path)
                                os.unlink(tmp_model_path)
                                
                                logger.info(f"✅ Loaded LSTM model successfully (fixed batch_shape issue)")
                                lstm_loaded = True
                            else:
                                raise Exception("No model_config in H5 file")
                    except Exception as e3:
                        logger.debug(f"Config fix method failed: {e3}")
                        
                        # Strategy 4: Last resort - try with older Keras API
                        try:
                            # Try using the legacy loading method
                            from tensorflow.python.keras.saving import hdf5_format
                            MODELS['lstm'] = hdf5_format.load_model_from_hdf5(lstm_path, custom_objects={}, compile=False)
                            logger.info(f"✅ Loaded LSTM model successfully (legacy HDF5 method)")
                            lstm_loaded = True
                        except Exception as e4:
                            logger.warning(f"⚠️ All LSTM loading strategies failed.")
                            logger.warning(f"⚠️ Errors: Standard={type(e1).__name__}, SafeMode={type(e2).__name__}, ConfigFix={type(e3).__name__}, Legacy={type(e4).__name__}")
                            logger.warning(f"⚠️ LSTM model may be incompatible with TensorFlow {tf.__version__}")
                            logger.info("⚠️ LSTM will be skipped. Other models (CatBoost, LightGBM, XGBoost) will work fine.")
            
            if not lstm_loaded:
                logger.warning("⚠️ LSTM model could not be loaded - skipping")
        else:
            logger.warning(f"⚠️ LSTM model file not found: {lstm_path}")
    except ImportError:
        logger.warning("⚠️ TensorFlow not installed, skipping LSTM model")
    except Exception as e:
        logger.error(f"⚠️ Error loading LSTM model: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        logger.info("⚠️ LSTM model will be skipped. Other models (CatBoost, LightGBM, XGBoost) will work fine.")
    
    FEATURE_COLUMNS = joblib.load(feature_cols_path)
    
    model_count = len(MODELS)
    logger.info(f"✅ Loaded {model_count} ML models successfully: {list(MODELS.keys())}")
    logger.info(f"✅ Feature columns: {len(FEATURE_COLUMNS)}")
except Exception as e:
    logger.error(f"❌ Error loading models: {e}")
    MODELS = None
    FEATURE_COLUMNS = None

# ============================================================================
# GEMINI API INTEGRATION FOR PREDICTION REASONING
# ============================================================================

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_API_URL = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent'

def generate_prediction_reasoning(prediction_data: Dict[str, Any]) -> str:
    """Generate AI reasoning for a prediction using Gemini API"""
    if not GEMINI_API_KEY:
        return ""  # Return empty if API key not configured
    
    try:
        # Prepare context for Gemini
        timestamp = prediction_data.get('timestamp', '')
        hour = prediction_data.get('hour', 0)
        predicted_load = prediction_data.get('predicted_load', 0)
        is_festival = prediction_data.get('is_festival', 0)
        festival_name = prediction_data.get('festival_name', 'None')
        boost = prediction_data.get('boost', 1.0)
        model = prediction_data.get('model', 'catboost')
        
        # Determine time of day
        if 6 <= hour < 12:
            time_period = "morning"
        elif 12 <= hour < 18:
            time_period = "afternoon"
        elif 18 <= hour < 22:
            time_period = "evening"
        else:
            time_period = "night"
        
        # Determine day of week
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            day_name = dt.strftime('%A')
            is_weekend = dt.weekday() >= 5
        except:
            day_name = "day"
            is_weekend = False
        
        # Build prompt
        prompt = f"""Analyze this traffic prediction and provide a brief, clear reasoning (2-3 sentences max):

Prediction Details:
- Time: {day_name} {time_period} (Hour {hour}:00)
- Predicted Traffic Load: {predicted_load:.0f} requests/hour
- Model Used: {model}
- Festival: {"Yes" if is_festival else "No"} ({festival_name})
- Festival Boost: {boost}x

Provide reasoning explaining:
1. Why the traffic is at this level for this time/day
2. Impact of festival (if applicable)
3. Expected user behavior patterns

Keep it concise and technical."""
        
        # Call Gemini API
        headers = {
            'Content-Type': 'application/json',
        }
        
        payload = {
            "contents": [{
                "parts": [{
                    "text": prompt
                }]
            }]
        }
        
        response = requests.post(
            f"{GEMINI_API_URL}?key={GEMINI_API_KEY}",
            headers=headers,
            json=payload,
            timeout=5
        )
        
        if response.status_code == 200:
            result = response.json()
            if 'candidates' in result and len(result['candidates']) > 0:
                reasoning = result['candidates'][0]['content']['parts'][0]['text']
                return reasoning.strip()
        
        logger.warning(f"Gemini API returned status {response.status_code}")
        return ""
        
    except Exception as e:
        logger.debug(f"Gemini API error: {e}")
        return ""  # Return empty on error - don't break predictions

# ============================================================================
# CALENDARIFIC API INTEGRATION
# ============================================================================

CALENDARIFIC_API_KEY = os.environ.get('CALENDARIFIC_API_KEY', '')
CALENDARIFIC_BASE_URL = 'https://calendarific.com/api/v2'

# Major festivals with traffic boost multipliers (from training data)
INDIAN_FESTIVALS = {
    # 2023
    '2023-01-26': {'name': 'Republic Day', 'boost': 2.5},
    '2023-03-08': {'name': 'Holi', 'boost': 3.0},
    '2023-04-14': {'name': 'Ram Navami', 'boost': 2.0},
    '2023-08-15': {'name': 'Independence Day', 'boost': 2.8},
    '2023-10-24': {'name': 'Diwali', 'boost': 4.5},
    '2023-11-12': {'name': 'Diwali Weekend', 'boost': 4.0},
    '2023-12-25': {'name': 'Christmas', 'boost': 3.2},
    
    # 2024
    '2024-01-26': {'name': 'Republic Day', 'boost': 2.5},
    '2024-03-25': {'name': 'Holi', 'boost': 3.0},
    '2024-04-17': {'name': 'Ram Navami', 'boost': 2.2},
    '2024-08-15': {'name': 'Independence Day', 'boost': 2.8},
    '2024-11-01': {'name': 'Diwali', 'boost': 4.5},
    '2024-11-15': {'name': 'Diwali Weekend', 'boost': 3.8},
    '2024-12-25': {'name': 'Christmas', 'boost': 3.2},
    
    # 2025 (extrapolated from 2024 patterns)
    '2025-01-26': {'name': 'Republic Day', 'boost': 2.5},
    '2025-03-14': {'name': 'Holi', 'boost': 3.0},
    '2025-04-06': {'name': 'Ram Navami', 'boost': 2.2},
    '2025-08-15': {'name': 'Independence Day', 'boost': 2.8},
    '2025-10-20': {'name': 'Diwali', 'boost': 4.5},
    '2025-10-21': {'name': 'Diwali Weekend', 'boost': 4.0},
    '2025-12-25': {'name': 'Christmas', 'boost': 3.2},
    
    # 2026 (extrapolated from patterns)
    '2026-01-26': {'name': 'Republic Day', 'boost': 2.5},
    '2026-03-03': {'name': 'Holi', 'boost': 3.0},
    '2026-03-27': {'name': 'Ram Navami', 'boost': 2.2},
    '2026-08-15': {'name': 'Independence Day', 'boost': 2.8},
    '2026-11-08': {'name': 'Diwali', 'boost': 4.5},
    '2026-11-09': {'name': 'Diwali Weekend', 'boost': 4.0},
    '2026-12-25': {'name': 'Christmas', 'boost': 3.2},
}

# Backward compatibility - convert to simple dict for existing functions
HARDCODED_FESTIVALS = {k: v['name'] for k, v in INDIAN_FESTIVALS.items()}

def check_festival_calendarific(date_str: str, country: str = 'IN') -> Dict[str, Any]:
    """Check if date is a festival using Calendarific API with hardcoded fallback and caching"""
    try:
        # Validate date format
        if not date_str or len(date_str) < 10:
            logger.warning(f"Invalid date format: '{date_str}'")
            return {'is_festival': 0, 'festival_name': 'None', 'boost': 1.0, 'all_festivals': []}
        
        # Check cache first (fast path)
        cache_key = f"{date_str}_{country}"
        current_time = datetime.now(timezone.utc).timestamp()
        
        if cache_key in FESTIVAL_CACHE:
            cached_result, cached_time = FESTIVAL_CACHE[cache_key]
            if current_time - cached_time < FESTIVAL_CACHE_TTL:
                return cached_result
            else:
                # Expired, remove from cache
                del FESTIVAL_CACHE[cache_key]
        
        date_obj = datetime.strptime(date_str, '%Y-%m-%d')
        year = date_obj.year
        month = date_obj.month
        day = date_obj.day
        
        # First check our Indian festivals list with boost values (fastest - no API call)
        if date_str in INDIAN_FESTIVALS:
            festival_data = INDIAN_FESTIVALS[date_str]
            result = {
                'is_festival': 1,
                'festival_name': festival_data['name'],
                'boost': festival_data['boost'],
                'all_festivals': [festival_data['name']]
            }
            # Cache the result
            FESTIVAL_CACHE[cache_key] = (result, current_time)
            return result
        
        # Try API if key is available (only for dates not in hardcoded list)
        if CALENDARIFIC_API_KEY:
            url = f"{CALENDARIFIC_BASE_URL}/holidays"
            params = {
                'api_key': CALENDARIFIC_API_KEY,
                'country': country,
                'year': year,
                'month': month,
                'day': day
            }
            
            try:
                # Reduced timeout for faster failure
                response = requests.get(url, params=params, timeout=3)
                
                if response.status_code == 200:
                    data = response.json()
                    
                    # Check if API returned valid data
                    if data.get('meta', {}).get('code') == 200:
                        holidays = data.get('response', {}).get('holidays', [])
                        
                        if holidays:
                            festival_names = [h['name'] for h in holidays]
                            result = {
                                'is_festival': 1,
                                'festival_name': festival_names[0],
                                'boost': 1.5,  # Default boost for other festivals
                                'all_festivals': festival_names
                            }
                            # Cache the result
                            FESTIVAL_CACHE[cache_key] = (result, current_time)
                            return result
            except Exception as api_error:
                logger.debug(f"Calendarific API error for {date_str}: {api_error}, using fallback")
        
        # Default: not a festival
        result = {'is_festival': 0, 'festival_name': 'None', 'boost': 1.0, 'all_festivals': []}
        # Cache the result (even negative results to avoid repeated API calls)
        FESTIVAL_CACHE[cache_key] = (result, current_time)
        return result
        
    except Exception as e:
        logger.warning(f"Calendarific API error: {e}")
        return {'is_festival': 0, 'festival_name': 'None', 'boost': 1.0, 'all_festivals': []}

# ============================================================================
# AWS AUTO SCALING INTEGRATION
# ============================================================================

def get_aws_autoscaling_client():
    """Get AWS Auto Scaling client"""
    # Prefer IAM role credentials (EC2 instance profile). If explicit keys are provided
    # in the environment we will use them; otherwise boto3 will automatically pick up
    # the instance role credentials when running on EC2. Returning a client (not None)
    # allows real scaling to proceed when running on an instance with an attached role.
    aws_access_key = os.environ.get('AWS_ACCESS_KEY_ID', '')
    aws_secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY', '')
    aws_region = os.environ.get('AWS_REGION', 'us-east-1')

    if aws_access_key and aws_secret_key:
        return boto3.client(
            'autoscaling',
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_key,
            region_name=aws_region
        )

    # No explicit keys: rely on IAM role / default session
    try:
        return boto3.client('autoscaling', region_name=aws_region)
    except Exception as e:
        logger.warning(f"Could not create boto3 autoscaling client: {e}")
        return None

def scale_ec2_instances(predicted_load: float, asg_name: str) -> Dict[str, Any]:
    """Scale EC2 Auto Scaling Group based on predicted load
    
    Scaling Logic:
    - < 700: 1 instance
    - 700-1400: 2 instances
    - 1400-2100: 3 instances
    - 2100-3000: 4 instances
    - 3000-5000: 5 instances
    - > 5000: 10 instances
    """
    try:
        client = get_aws_autoscaling_client()

        # Calculate recommended instances based on traffic load
        recommended = calculate_recommended_instances(predicted_load)

        # Configurable safeguards (environment variables)
        max_increment = int(os.environ.get('MAX_SCALE_INCREMENT', '2'))  # max new instances per call
        hard_cap = int(os.environ.get('HARD_MAX_CAP', '2'))  # absolute maximum instances allowed
        cooldown_seconds = int(os.environ.get('SCALE_COOLDOWN_SECONDS', '300'))

        # fallback max_size mapping for backward compatibility
        max_size_map = {1: 2, 2: 3, 3: 5, 4: 6, 5: 8, 10: 15}
        max_size = max_size_map.get(recommended, max(hard_cap, 15))

        if not client:
            # Mock mode (no AWS credentials or client creation failed)
            logger.info("[MOCK] AWS autoscaling client not available; running in mock mode")
            desired = min(recommended, hard_cap)
            return {
                'success': True,
                'mode': 'mock',
                'predicted_load': predicted_load,
                'recommended_capacity': recommended,
                'desired_capacity': desired,
                'max_size': max_size,
                'message': f'[MOCK] Would scale to {desired} instances for load {predicted_load:.0f}',
            }

        # Describe ASG to get current desired and instance counts
        resp = client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        groups = resp.get('AutoScalingGroups', [])
        if not groups:
            raise ClientError({'Error': {'Message': f'ASG {asg_name} not found'}}, 'DescribeAutoScalingGroups')

        group = groups[0]
        current_desired = int(group.get('DesiredCapacity', 0))
        in_service = sum(1 for i in group.get('Instances', []) if i.get('LifecycleState') == 'InService')
        min_size = int(group.get('MinSize', 1))
        current_max = int(group.get('MaxSize', max_size))

        # Enforce hard cap
        effective_hard_cap = min(hard_cap, current_max)

        # Respect cooldown: simple in-memory guard (per ASG)
        now = datetime.now().timestamp()
        last_scale = SCALE_STATE.get(asg_name, {}).get('last_scale_time', 0)
        if now - last_scale < cooldown_seconds:
            logger.info(f"Scale request for {asg_name} within cooldown ({now-last_scale:.0f}s); skipping scale")
            return {
                'success': True,
                'mode': 'throttled',
                'predicted_load': predicted_load,
                'recommended_capacity': recommended,
                'current_desired': current_desired,
                'in_service': in_service,
                'message': f'Scale request skipped due to cooldown ({int(now-last_scale)}s elapsed)'
            }

        # Compute desired safely: do not exceed hard cap and only increment by max_increment
        if recommended > current_desired:
            # scale up but limit jump
            desired = min(recommended, current_desired + max_increment, effective_hard_cap)
        else:
            # allow scale down to recommended (but not below min_size)
            desired = max(recommended, min_size)

        # If desired equals current_desired, nothing to do
        if desired == current_desired:
            logger.info(f"No scaling action required for {asg_name} (current: {current_desired}, desired: {desired})")
            return {
                'success': True,
                'mode': 'noop',
                'predicted_load': predicted_load,
                'recommended_capacity': recommended,
                'current_desired': current_desired,
                'desired_capacity': desired,
                'message': 'No scaling change required'
            }

        # Apply desired capacity change (honor cooldown)
        logger.info(f"Scaling ASG {asg_name}: current={current_desired}, in_service={in_service}, recommended={recommended}, desired={desired}, hard_cap={effective_hard_cap}")
        client.set_desired_capacity(
            AutoScalingGroupName=asg_name,
            DesiredCapacity=int(desired),
            HonorCooldown=True
        )

        # Ensure MaxSize not below desired and not above effective_hard_cap
        new_max = max(current_max, desired)
        new_max = min(new_max, effective_hard_cap)
        if new_max != current_max:
            client.update_auto_scaling_group(
                AutoScalingGroupName=asg_name,
                MaxSize=int(new_max)
            )

        # Record last scale time
        SCALE_STATE.setdefault(asg_name, {})['last_scale_time'] = now

        return {
            'success': True,
            'mode': 'real',
            'predicted_load': predicted_load,
            'recommended_capacity': recommended,
            'current_desired': current_desired,
            'desired_capacity': int(desired),
            'max_size': int(new_max),
            'asg_name': asg_name,
            'message': f'Scaled {asg_name} to {int(desired)} instances'
        }
        
    except (ClientError, NoCredentialsError) as e:
        logger.error(f"AWS scaling error: {e}")
        return {
            'success': False,
            'error': str(e),
            'message': 'Failed to scale AWS instances'
        }

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def calculate_recommended_instances(peak_load: float) -> int:
    """Calculate recommended instances based on peak load"""
    if peak_load < 700:
        return 1
    elif peak_load < 1400:
        return 2
    elif peak_load < 2100:
        return 3
    elif peak_load < 3000:
        return 4
    elif peak_load < 5000:
        return 5
    else:
        return 10

def get_peak_prediction_with_fallback(timestamp: datetime, model_name: str, boost: float) -> tuple:
    """Get peak hour prediction with fallback to CatBoost. Returns (avg_load, peak_load)"""
    try:
        peak_pred = predict_traffic(timestamp, model_name, use_cache=True)
        peak_prediction = peak_pred['predicted_load']
        return peak_prediction * 0.85, peak_prediction * 1.15
    except Exception as e:
        logger.warning(f"Error predicting with {model_name}: {e}")
        if model_name != 'catboost':
            try:
                peak_pred = predict_traffic(timestamp, 'catboost', use_cache=True)
                peak_prediction = peak_pred['predicted_load']
                return peak_prediction * 0.85, peak_prediction * 1.15
            except:
                base_load = 1000.0
                return base_load * boost, base_load * boost * 1.2
        else:
            return 1200.0, 1500.0

# ============================================================================
# FEATURE ENGINEERING
# ============================================================================

def prepare_features_for_hour(timestamp: datetime, festival_info: Dict) -> pd.DataFrame:
    """Prepare features for a single hour prediction - optimized"""
    hour = timestamp.hour
    day_of_week = timestamp.weekday()
    month = timestamp.month
    
    # Pre-compute cyclical encoding (faster than sin/cos on each call)
    hour_sin = np.sin(2 * np.pi * hour / 24)
    hour_cos = np.cos(2 * np.pi * hour / 24)
    dow_sin = np.sin(2 * np.pi * day_of_week / 7)
    dow_cos = np.cos(2 * np.pi * day_of_week / 7)
    
    # Fast boolean calculations
    is_weekend = int(day_of_week >= 5)
    is_business_hours = int(9 <= hour <= 18)
    is_peak_hours = int(hour in (12, 13, 19, 20, 21))
    is_night = int(hour < 6 or hour > 22)
    
    # Festival info
    is_festival = festival_info.get('is_festival', 0)
    festival_name = festival_info.get('festival_name', 'None')
    
    # Use dict literal for faster DataFrame creation
    features = {
        'hour': hour,
        'day_of_week': day_of_week,
        'month': month,
        'day': timestamp.day,
        'year': timestamp.year,
        'week_of_year': timestamp.isocalendar()[1],
        'quarter': (month - 1) // 3 + 1,
        'day_of_year': timestamp.timetuple().tm_yday,
        'hour_sin': hour_sin,
        'hour_cos': hour_cos,
        'dow_sin': dow_sin,
        'dow_cos': dow_cos,
        'is_weekend': is_weekend,
        'is_business_hours': is_business_hours,
        'is_peak_hours': is_peak_hours,
        'is_night': is_night,
        'is_festival': is_festival,
        'is_campaign': 0,
        'festival_name': festival_name,
        'traffic_lag_1h': 1200.0,
        'traffic_lag_24h': 1500.0,
        'traffic_lag_168h': 1400.0,
        'traffic_rolling_mean_24h': 1300.0,
        'traffic_rolling_std_24h': 200.0,
        'traffic_rolling_max_24h': 2000.0,
        'cpu_usage': 45.0,
        'memory_usage': 60.0,
        'response_time': 150.0,
        'error_rate': 0.5
    }
    
    # Create DataFrame directly from dict (faster than list of dicts)
    return pd.DataFrame([features])

# ============================================================================
# PREDICTION FUNCTION
# ============================================================================

def predict_traffic(timestamp: datetime, model_name: str = 'catboost', use_cache: bool = True) -> Dict[str, Any]:
    """Predict traffic for a given timestamp using specified model"""
    
    if not MODELS or not FEATURE_COLUMNS:
        raise HTTPException(status_code=500, detail="Models not loaded")
    
    # Check if model has failed too many times - auto-fallback to CatBoost
    if model_name != 'catboost' and model_name in MODEL_FAILURE_COUNT:
        if MODEL_FAILURE_COUNT[model_name] >= MAX_FAILURES:
            logger.debug(f"Model {model_name} has failed {MODEL_FAILURE_COUNT[model_name]} times, using CatBoost instead")
            model_name = 'catboost'
    
    if model_name not in MODELS:
        raise HTTPException(status_code=400, detail=f"Model '{model_name}' not found")
    
    # Check cache first (optimized)
    if use_cache:
        cache_key = get_cache_key(timestamp, model_name)
        if cache_key in PREDICTION_CACHE:
            cached_result, cached_time = PREDICTION_CACHE[cache_key]
            current_time = datetime.now(timezone.utc).timestamp()
            # Quick TTL check without full cleanup
            if current_time - cached_time < CACHE_TTL:
                return cached_result
            else:
                # Expired, remove from cache
                del PREDICTION_CACHE[cache_key]
        
        # Periodic cleanup (only every 5 minutes)
        clear_old_cache()
    
    # Check festival
    date_str = timestamp.strftime('%Y-%m-%d')
    festival_info = check_festival_calendarific(date_str)
    
    # Prepare features
    features_df = prepare_features_for_hour(timestamp, festival_info)
    
    # Ensure columns match training
    features_df = features_df[FEATURE_COLUMNS]
    
    # Predict
    model = MODELS[model_name]
    
    if model_name == 'catboost':
        prediction = model.predict(features_df)[0]
    elif model_name == 'lightgbm':
        # LightGBM needs categorical features to be handled properly
        try:
            # Convert festival_name to numeric if it exists
            features_for_lgbm = features_df.copy()
            if 'festival_name' in features_for_lgbm.columns:
                # Map festival names to numeric values
                festival_map = {
                    'None': 0, 'Diwali': 1, 'Holi': 2, 'Christmas': 3,
                    'Independence Day': 4, 'Republic Day': 5, 'Ram Navami': 6, 'Diwali Weekend': 7
                }
                features_for_lgbm['festival_name'] = features_for_lgbm['festival_name'].map(
                    lambda x: festival_map.get(str(x), 0)
                ).fillna(0).astype(int)
            
            # Ensure all columns are numeric for LightGBM
            for col in features_for_lgbm.columns:
                if pd.api.types.is_categorical_dtype(features_for_lgbm[col]):
                    features_for_lgbm[col] = features_for_lgbm[col].astype(int)
                elif features_for_lgbm[col].dtype == 'object':
                    features_for_lgbm[col] = pd.to_numeric(features_for_lgbm[col], errors='coerce').fillna(0)
                elif not pd.api.types.is_numeric_dtype(features_for_lgbm[col]):
                    features_for_lgbm[col] = pd.to_numeric(features_for_lgbm[col], errors='coerce').fillna(0)
            
            # Use appropriate prediction method
            if hasattr(model, 'best_iteration'):
                prediction = model.predict(features_for_lgbm, num_iteration=model.best_iteration)[0]
            else:
                prediction = model.predict(features_for_lgbm)[0]
            
            # Reset failure count on success
            if 'lightgbm' in MODEL_FAILURE_COUNT:
                MODEL_FAILURE_COUNT['lightgbm'] = 0
            logger.debug(f"LightGBM prediction successful: {prediction}")
        except Exception as e:
            # Track failure count
            if 'lightgbm' not in MODEL_FAILURE_COUNT:
                MODEL_FAILURE_COUNT['lightgbm'] = 0
            MODEL_FAILURE_COUNT['lightgbm'] += 1
            
            logger.warning(f"LightGBM prediction error: {e}, falling back to CatBoost (failure count: {MODEL_FAILURE_COUNT['lightgbm']})")
            # Fallback to CatBoost if LightGBM fails
            if 'catboost' in MODELS:
                catboost_model = MODELS['catboost']
                prediction = catboost_model.predict(features_df)[0]
                model_name = 'catboost'  # Update model name for result
            else:
                raise Exception(f"LightGBM failed and CatBoost not available: {e}")
    elif model_name == 'xgboost':
        try:
            import xgboost as xgb
            
            # Suppress XGBoost warnings during prediction
            with warnings.catch_warnings():
                warnings.filterwarnings('ignore', category=UserWarning, module='xgboost')
                
                # Encode festival_name using fixed mapping (not LabelEncoder)
                features_encoded = features_df.copy()
                if 'festival_name' in features_encoded.columns:
                    festival_val = str(features_encoded['festival_name'].iloc[0])
                    encoded_val = encode_festival_name_for_xgboost(festival_val)
                    features_encoded['festival_name'] = encoded_val
                
                # Ensure all columns are numeric and in correct order
                for col in features_encoded.columns:
                    if not pd.api.types.is_numeric_dtype(features_encoded[col]):
                        features_encoded[col] = pd.to_numeric(features_encoded[col], errors='coerce').fillna(0)
                
                # Ensure feature order matches training (use FEATURE_COLUMNS order)
                features_encoded = features_encoded[FEATURE_COLUMNS]
                
                # Create DMatrix with feature names for better compatibility
                dmatrix = xgb.DMatrix(features_encoded, feature_names=FEATURE_COLUMNS)
                prediction = model.predict(dmatrix)[0]
                
                # Ensure prediction is positive (XGBoost might return negative values)
                prediction = max(prediction, 0.0)
                
                # Reset failure count on success
                if 'xgboost' in MODEL_FAILURE_COUNT:
                    MODEL_FAILURE_COUNT['xgboost'] = 0
                logger.debug(f"XGBoost prediction successful: {prediction}")
        except Exception as e:
            # Track failure count
            if 'xgboost' not in MODEL_FAILURE_COUNT:
                MODEL_FAILURE_COUNT['xgboost'] = 0
            MODEL_FAILURE_COUNT['xgboost'] += 1
            
            logger.warning(f"XGBoost prediction error: {e}, falling back to CatBoost (failure count: {MODEL_FAILURE_COUNT['xgboost']})")
            # Fallback to CatBoost if XGBoost fails
            if 'catboost' in MODELS:
                catboost_model = MODELS['catboost']
                prediction = catboost_model.predict(features_df)[0]
                model_name = 'catboost'  # Update model name for result
            else:
                raise Exception(f"XGBoost failed and CatBoost not available: {e}")
    elif model_name == 'lstm':
        try:
            import tensorflow as tf
            
            # LSTM models need numeric features - encode festival_name
            features_for_lstm = features_df.copy()
            if 'festival_name' in features_for_lstm.columns:
                # Use same encoding as XGBoost for consistency
                features_for_lstm['festival_name'] = features_for_lstm['festival_name'].astype(str).map(
                    lambda x: encode_festival_name_for_xgboost(x)
                ).fillna(0).astype(int)
            
            # Ensure all columns are numeric
            for col in features_for_lstm.columns:
                if not pd.api.types.is_numeric_dtype(features_for_lstm[col]):
                    features_for_lstm[col] = pd.to_numeric(features_for_lstm[col], errors='coerce').fillna(0)
            
            # Prepare features as numpy array in correct order
            features_array = features_for_lstm[FEATURE_COLUMNS].values.astype(np.float32)
            
            # Reshape for LSTM: (samples, timesteps, features)
            # For single prediction, we use 1 timestep with all features
            # If model expects sequence, we'll need to adjust
            if len(features_array.shape) == 2:
                # Reshape to (1, 1, n_features) for LSTM
                features_array = features_array.reshape(1, 1, -1)
            
            prediction = model.predict(features_array, verbose=0)[0][0]
            
            # Ensure prediction is positive and scalar
            if isinstance(prediction, (list, np.ndarray)):
                prediction = float(prediction[0] if len(prediction) > 0 else prediction)
            else:
                prediction = float(prediction)
            
            prediction = max(prediction, 0.0)
            
            # Reset failure count on success
            if 'lstm' in MODEL_FAILURE_COUNT:
                MODEL_FAILURE_COUNT['lstm'] = 0
            logger.debug(f"LSTM prediction successful: {prediction}")
        except Exception as e:
            # Track failure count
            if 'lstm' not in MODEL_FAILURE_COUNT:
                MODEL_FAILURE_COUNT['lstm'] = 0
            MODEL_FAILURE_COUNT['lstm'] += 1
            
            logger.warning(f"LSTM prediction error: {e}, falling back to CatBoost (failure count: {MODEL_FAILURE_COUNT['lstm']})")
            # Fallback to CatBoost if LSTM fails
            if 'catboost' in MODELS:
                catboost_model = MODELS['catboost']
                prediction = catboost_model.predict(features_df)[0]
                model_name = 'catboost'  # Update model name for result
            else:
                raise Exception(f"LSTM failed and CatBoost not available: {e}")
    
    # Ensure positive prediction
    prediction = max(prediction, 50.0)
    
    # Apply festival boost multiplier
    boost = festival_info.get('boost', 1.0)
    if boost > 1.0:
        prediction = prediction * boost
    
    result = {
        'timestamp': timestamp.isoformat(),
        'hour': timestamp.hour,
        'predicted_load': float(prediction),
        'is_festival': festival_info['is_festival'],
        'festival_name': festival_info['festival_name'],
        'boost': boost,
        'model': model_name,
        'reasoning': ''  # Will be filled below
    }
    
    # Generate AI reasoning for single prediction
    try:
        reasoning = generate_prediction_reasoning(result)
        result['reasoning'] = reasoning
    except Exception as e:
        logger.debug(f"Failed to generate reasoning: {e}")
        result['reasoning'] = ""
    
    # Cache the result
    if use_cache:
        cache_key = get_cache_key(timestamp, model_name)
        PREDICTION_CACHE[cache_key] = (result, datetime.now(timezone.utc).timestamp())
    
    return result

# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class PredictionRequest(BaseModel):
    start_time: str  # ISO format datetime
    hours: int = 24
    model_name: str = 'catboost'

class PredictionResponse(BaseModel):
    timestamp: str
    hour: int
    predicted_load: float
    is_festival: int
    festival_name: str
    model: str
    reasoning: str = ""  # AI-generated reasoning for the prediction

class ScalingRequest(BaseModel):
    predicted_load: float
    asg_name: str = os.environ.get('AWS_ASG_NAME', 'my-asg')

class StatusCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class StatusCheckCreate(BaseModel):
    client_name: str

# ============================================================================
# API ROUTES
# ============================================================================

@api_router.get("/")
async def root():
    return {
        "message": "AI Predictive Autoscaling System",
        "models_loaded": MODELS is not None,
        "available_models": list(MODELS.keys()) if MODELS else []
    }

@api_router.get("/health")
async def health_check():
    aws_configured = bool(os.environ.get('AWS_ACCESS_KEY_ID') and os.environ.get('AWS_SECRET_ACCESS_KEY'))
    gemini_configured = bool(GEMINI_API_KEY)
    mongo_configured = bool(client and db)
    return {
        "status": "healthy",
        "models_loaded": MODELS is not None,
        "feature_columns": len(FEATURE_COLUMNS) if FEATURE_COLUMNS else 0,
        "aws_configured": aws_configured,
        "aws_region": os.environ.get('AWS_REGION', 'not-set'),
        "gemini_configured": gemini_configured,
        "mongo_configured": mongo_configured,
        "total_festivals_2025": len([k for k in HARDCODED_FESTIVALS.keys() if k.startswith('2025')])
    }

@api_router.post("/predict", response_model=List[PredictionResponse])
async def predict_endpoint(request: PredictionRequest):
    """Predict traffic for next N hours - optimized with batch processing and smart caching"""
    start_time = parse_iso_datetime(request.start_time)
    
    # Batch prepare all timestamps first
    all_timestamps = [start_time + timedelta(hours=i) for i in range(request.hours)]
    
    # Check if model has failed too many times - auto-fallback to CatBoost
    actual_model_name = request.model_name
    if request.model_name != 'catboost' and request.model_name in MODEL_FAILURE_COUNT:
        if MODEL_FAILURE_COUNT[request.model_name] >= MAX_FAILURES:
            logger.info(f"Model {request.model_name} has failed {MODEL_FAILURE_COUNT[request.model_name]} times, using CatBoost")
            actual_model_name = 'catboost'
    
    # Check cache first for all timestamps - much faster (optimized)
    clear_old_cache()  # Only cleans if needed (every 5 min)
    cached_predictions = {}
    uncached_timestamps = []
    current_time = datetime.now(timezone.utc).timestamp()
    
    for ts in all_timestamps:
        cache_key = get_cache_key(ts, actual_model_name)
        if cache_key in PREDICTION_CACHE:
            cached_result, cached_time = PREDICTION_CACHE[cache_key]
            # Quick TTL check (avoid full cleanup on every request)
            if current_time - cached_time < CACHE_TTL:
                cached_predictions[ts] = cached_result
            else:
                # Expired, remove and add to uncached
                del PREDICTION_CACHE[cache_key]
                uncached_timestamps.append(ts)
        else:
            uncached_timestamps.append(ts)
    
    # If all cached, return immediately (instant response!)
    if len(cached_predictions) == len(all_timestamps):
        return [cached_predictions[ts] for ts in all_timestamps]
    
    # Batch prepare festival info only for uncached timestamps
    festival_info_cache = {}
    for ts in uncached_timestamps:
        date_str = ts.strftime('%Y-%m-%d')
        if date_str not in festival_info_cache:
            festival_info_cache[date_str] = check_festival_calendarific(date_str)
    
    try:
        # Batch predict only if we have uncached timestamps
        if not uncached_timestamps:
            # All cached, return cached results
            return [cached_predictions[ts] for ts in all_timestamps]
        
        # Prepare features in batch - much faster
        features_dicts = []
        for ts in uncached_timestamps:
            date_str = ts.strftime('%Y-%m-%d')
            hour = ts.hour
            day_of_week = ts.weekday()
            month = ts.month
            festival_info = festival_info_cache[date_str]
            
            # Fast feature calculation
            features_dicts.append({
                'hour': hour,
                'day_of_week': day_of_week,
                'month': month,
                'day': ts.day,
                'year': ts.year,
                'week_of_year': ts.isocalendar()[1],
                'quarter': (month - 1) // 3 + 1,
                'day_of_year': ts.timetuple().tm_yday,
                'hour_sin': np.sin(2 * np.pi * hour / 24),
                'hour_cos': np.cos(2 * np.pi * hour / 24),
                'dow_sin': np.sin(2 * np.pi * day_of_week / 7),
                'dow_cos': np.cos(2 * np.pi * day_of_week / 7),
                'is_weekend': int(day_of_week >= 5),
                'is_business_hours': int(9 <= hour <= 18),
                'is_peak_hours': int(hour in (12, 13, 19, 20, 21)),
                'is_night': int(hour < 6 or hour > 22),
                'is_festival': festival_info.get('is_festival', 0),
                'is_campaign': 0,
                'festival_name': festival_info.get('festival_name', 'None'),
                'traffic_lag_1h': 1200.0,
                'traffic_lag_24h': 1500.0,
                'traffic_lag_168h': 1400.0,
                'traffic_rolling_mean_24h': 1300.0,
                'traffic_rolling_std_24h': 200.0,
                'traffic_rolling_max_24h': 2000.0,
                'cpu_usage': 45.0,
                'memory_usage': 60.0,
                'response_time': 150.0,
                'error_rate': 0.5
            })
        
        # Create DataFrame from list of dicts in one go (much faster)
        all_features = pd.DataFrame(features_dicts)
        all_features = all_features[FEATURE_COLUMNS]
        
        # Batch predict (much faster than individual predictions)
        model = MODELS[actual_model_name]
        predictions_raw = []
        
        if actual_model_name == 'catboost':
            # Batch predict all at once
            predictions_raw = model.predict(all_features)
        elif actual_model_name == 'lightgbm':
            # LightGBM batch processing - handle categorical features properly
            try:
                # Convert all categorical/object columns to numeric FIRST
                all_features_lgbm = all_features.copy()
                if 'festival_name' in all_features_lgbm.columns:
                    # Fast map using replace (faster than map)
                    festival_map = {
                        'None': 0, 'Diwali': 1, 'Holi': 2, 'Christmas': 3,
                        'Independence Day': 4, 'Republic Day': 5, 'Ram Navami': 6, 'Diwali Weekend': 7
                    }
                    all_features_lgbm['festival_name'] = all_features_lgbm['festival_name'].replace(festival_map).fillna(0).astype(int)
                
                # Ensure all columns are numeric
                for col in all_features_lgbm.columns:
                    if pd.api.types.is_categorical_dtype(all_features_lgbm[col]):
                        all_features_lgbm[col] = all_features_lgbm[col].astype(int)
                    elif all_features_lgbm[col].dtype == 'object':
                        all_features_lgbm[col] = pd.to_numeric(all_features_lgbm[col], errors='coerce').fillna(0)
                
                # Predict with LightGBM
                if hasattr(model, 'best_iteration'):
                    predictions_raw = model.predict(all_features_lgbm, num_iteration=model.best_iteration)
                else:
                    predictions_raw = model.predict(all_features_lgbm)
                
                # Reset failure count on success
                if 'lightgbm' in MODEL_FAILURE_COUNT:
                    MODEL_FAILURE_COUNT['lightgbm'] = 0
                logger.info(f"LightGBM batch prediction successful: {len(predictions_raw)} predictions")
            except Exception as lgbm_error:
                # Track failure count
                if 'lightgbm' not in MODEL_FAILURE_COUNT:
                    MODEL_FAILURE_COUNT['lightgbm'] = 0
                MODEL_FAILURE_COUNT['lightgbm'] += 1
                
                logger.warning(f"LightGBM batch prediction failed: {lgbm_error}, falling back to CatBoost (failure count: {MODEL_FAILURE_COUNT['lightgbm']})")
                # Fallback to CatBoost for reliability
                model = MODELS['catboost']
                predictions_raw = model.predict(all_features)
                actual_model_name = 'catboost'  # Update for result
        elif actual_model_name == 'xgboost':
            try:
                import xgboost as xgb
                
                # Suppress XGBoost warnings during batch prediction
                with warnings.catch_warnings():
                    warnings.filterwarnings('ignore', category=UserWarning, module='xgboost')
                    
                    all_features_xgb = all_features.copy()
                    if 'festival_name' in all_features_xgb.columns:
                        # Use fixed mapping instead of LabelEncoder
                        all_features_xgb['festival_name'] = all_features_xgb['festival_name'].astype(str).map(
                            lambda x: encode_festival_name_for_xgboost(x)
                        ).fillna(0).astype(int)
                    
                    # Ensure all columns are numeric
                    for col in all_features_xgb.columns:
                        if not pd.api.types.is_numeric_dtype(all_features_xgb[col]):
                            all_features_xgb[col] = pd.to_numeric(all_features_xgb[col], errors='coerce').fillna(0)
                    
                    # Ensure feature order matches training
                    all_features_xgb = all_features_xgb[FEATURE_COLUMNS]
                    
                    # Create DMatrix with feature names
                    dmatrix = xgb.DMatrix(all_features_xgb, feature_names=FEATURE_COLUMNS)
                    predictions_raw = model.predict(dmatrix)
                    
                    # Ensure all predictions are positive
                    predictions_raw = np.maximum(predictions_raw, 0.0)
                    
                    # Reset failure count on success
                    if 'xgboost' in MODEL_FAILURE_COUNT:
                        MODEL_FAILURE_COUNT['xgboost'] = 0
                    logger.info(f"XGBoost batch prediction successful: {len(predictions_raw)} predictions")
            except Exception as xgb_error:
                # Track failure count
                if 'xgboost' not in MODEL_FAILURE_COUNT:
                    MODEL_FAILURE_COUNT['xgboost'] = 0
                MODEL_FAILURE_COUNT['xgboost'] += 1
                
                logger.warning(f"XGBoost batch prediction failed: {xgb_error}, falling back to CatBoost (failure count: {MODEL_FAILURE_COUNT['xgboost']})")
                # Fallback to CatBoost for reliability
                model = MODELS['catboost']
                predictions_raw = model.predict(all_features)
                actual_model_name = 'catboost'  # Update for result
        elif actual_model_name == 'lstm':
            try:
                import tensorflow as tf
                
                # LSTM models need numeric features - encode festival_name
                all_features_lstm = all_features.copy()
                if 'festival_name' in all_features_lstm.columns:
                    # Use same encoding as XGBoost for consistency
                    all_features_lstm['festival_name'] = all_features_lstm['festival_name'].astype(str).map(
                        lambda x: encode_festival_name_for_xgboost(x)
                    ).fillna(0).astype(int)
                
                # Ensure all columns are numeric
                for col in all_features_lstm.columns:
                    if not pd.api.types.is_numeric_dtype(all_features_lstm[col]):
                        all_features_lstm[col] = pd.to_numeric(all_features_lstm[col], errors='coerce').fillna(0)
                
                # Prepare features for LSTM batch prediction in correct order
                features_array = all_features_lstm[FEATURE_COLUMNS].values.astype(np.float32)
                
                # Reshape for LSTM: (batch_size, timesteps, features)
                # For hourly predictions, each row is a timestep
                if len(features_array.shape) == 2:
                    # Reshape to (batch_size, 1, n_features) for LSTM
                    features_array = features_array.reshape(features_array.shape[0], 1, features_array.shape[1])
                
                predictions_raw = model.predict(features_array, verbose=0)
                
                # Flatten predictions if needed
                if len(predictions_raw.shape) > 1:
                    predictions_raw = predictions_raw.flatten()
                
                # Ensure all predictions are positive
                predictions_raw = np.maximum(predictions_raw.astype(np.float32), 0.0)
                
                # Reset failure count on success
                if 'lstm' in MODEL_FAILURE_COUNT:
                    MODEL_FAILURE_COUNT['lstm'] = 0
                logger.info(f"LSTM batch prediction successful: {len(predictions_raw)} predictions")
            except Exception as lstm_error:
                # Track failure count
                if 'lstm' not in MODEL_FAILURE_COUNT:
                    MODEL_FAILURE_COUNT['lstm'] = 0
                MODEL_FAILURE_COUNT['lstm'] += 1
                
                logger.warning(f"LSTM batch prediction failed: {lstm_error}, falling back to CatBoost (failure count: {MODEL_FAILURE_COUNT['lstm']})")
                # Fallback to CatBoost for reliability
                model = MODELS['catboost']
                predictions_raw = model.predict(all_features)
                actual_model_name = 'catboost'  # Update for result
        
        # Format results for uncached timestamps
        new_predictions = []
        for i, ts in enumerate(uncached_timestamps):
            date_str = ts.strftime('%Y-%m-%d')
            festival_info = festival_info_cache[date_str]
            prediction = max(float(predictions_raw[i]), 50.0)
            
            # Apply boost
            boost = festival_info.get('boost', 1.0)
            if boost > 1.0:
                prediction = prediction * boost
            
            result = {
                'timestamp': ts.isoformat(),
                'hour': ts.hour,
                'predicted_load': prediction,
                'is_festival': festival_info['is_festival'],
                'festival_name': festival_info['festival_name'],
                'boost': boost,
                'model': actual_model_name,  # Use actual model used (may be CatBoost fallback)
                'reasoning': ''  # Will be generated below
            }
            
            # Generate AI reasoning using Gemini (async, non-blocking)
            try:
                reasoning = generate_prediction_reasoning(result)
                result['reasoning'] = reasoning
            except Exception as e:
                logger.debug(f"Failed to generate reasoning: {e}")
                result['reasoning'] = ""
            
            # Cache the result
            cache_key = get_cache_key(ts, actual_model_name)
            PREDICTION_CACHE[cache_key] = (result, datetime.now(timezone.utc).timestamp())
            new_predictions.append(result)
        
        # Combine cached and new predictions in original order
        # Add reasoning to cached predictions if missing
        all_predictions = []
        for ts in [start_time + timedelta(hours=i) for i in range(request.hours)]:
            if ts in cached_predictions:
                pred = cached_predictions[ts]
                # Add reasoning to cached predictions if missing
                if not pred.get('reasoning'):
                    try:
                        reasoning = generate_prediction_reasoning(pred)
                        pred['reasoning'] = reasoning
                    except:
                        pred['reasoning'] = ""
                all_predictions.append(pred)
            else:
                all_predictions.append(new_predictions[uncached_timestamps.index(ts)])
        
        return all_predictions
        
    except Exception as e:
        logger.error(f"Prediction error: {e}")
        # Try fallback to CatBoost if other model fails
        if request.model_name != 'catboost' and MODELS and 'catboost' in MODELS:
            logger.info(f"Attempting fallback to CatBoost model")
            try:
                # Re-prepare features for fallback (only uncached timestamps)
                if not uncached_timestamps:
                    # If all were cached, return cached results
                    return [cached_predictions[ts] for ts in all_timestamps]
                
                # Prepare features for uncached timestamps
                fallback_features_dicts = []
                for ts in uncached_timestamps:
                    date_str = ts.strftime('%Y-%m-%d')
                    hour = ts.hour
                    day_of_week = ts.weekday()
                    month = ts.month
                    festival_info = festival_info_cache[date_str]
                    
                    fallback_features_dicts.append({
                        'hour': hour,
                        'day_of_week': day_of_week,
                        'month': month,
                        'day': ts.day,
                        'year': ts.year,
                        'week_of_year': ts.isocalendar()[1],
                        'quarter': (month - 1) // 3 + 1,
                        'day_of_year': ts.timetuple().tm_yday,
                        'hour_sin': np.sin(2 * np.pi * hour / 24),
                        'hour_cos': np.cos(2 * np.pi * hour / 24),
                        'dow_sin': np.sin(2 * np.pi * day_of_week / 7),
                        'dow_cos': np.cos(2 * np.pi * day_of_week / 7),
                        'is_weekend': int(day_of_week >= 5),
                        'is_business_hours': int(9 <= hour <= 18),
                        'is_peak_hours': int(hour in (12, 13, 19, 20, 21)),
                        'is_night': int(hour < 6 or hour > 22),
                        'is_festival': festival_info.get('is_festival', 0),
                        'is_campaign': 0,
                        'festival_name': festival_info.get('festival_name', 'None'),
                        'traffic_lag_1h': 1200.0,
                        'traffic_lag_24h': 1500.0,
                        'traffic_lag_168h': 1400.0,
                        'traffic_rolling_mean_24h': 1300.0,
                        'traffic_rolling_std_24h': 200.0,
                        'traffic_rolling_max_24h': 2000.0,
                        'cpu_usage': 45.0,
                        'memory_usage': 60.0,
                        'response_time': 150.0,
                        'error_rate': 0.5
                    })
                
                model = MODELS['catboost']
                all_features = pd.DataFrame(fallback_features_dicts)
                all_features = all_features[FEATURE_COLUMNS]
                predictions_raw = model.predict(all_features)
                
                # Format results with CatBoost
                new_predictions = []
                for i, ts in enumerate(uncached_timestamps):
                    date_str = ts.strftime('%Y-%m-%d')
                    festival_info = festival_info_cache[date_str]
                    prediction = max(float(predictions_raw[i]), 50.0)
                    
                    boost = festival_info.get('boost', 1.0)
                    if boost > 1.0:
                        prediction = prediction * boost
                    
                    result = {
                        'timestamp': ts.isoformat(),
                        'hour': ts.hour,
                        'predicted_load': prediction,
                        'is_festival': festival_info['is_festival'],
                        'festival_name': festival_info['festival_name'],
                        'boost': boost,
                        'model': 'catboost',  # Note: used fallback
                        'reasoning': ''  # Will be generated
                    }
                    
                    # Generate AI reasoning
                    try:
                        reasoning = generate_prediction_reasoning(result)
                        result['reasoning'] = reasoning
                    except Exception as e:
                        logger.debug(f"Failed to generate reasoning: {e}")
                        result['reasoning'] = ""
                    
                    # Cache the result
                    cache_key = get_cache_key(ts, 'catboost')
                    PREDICTION_CACHE[cache_key] = (result, datetime.now(timezone.utc).timestamp())
                    new_predictions.append(result)
                
                # Combine cached and new predictions
                # Add reasoning to cached predictions if missing
                all_predictions = []
                for ts in all_timestamps:
                    if ts in cached_predictions:
                        pred = cached_predictions[ts]
                        # Add reasoning to cached predictions if missing
                        if not pred.get('reasoning'):
                            try:
                                reasoning = generate_prediction_reasoning(pred)
                                pred['reasoning'] = reasoning
                            except:
                                pred['reasoning'] = ""
                        all_predictions.append(pred)
                    else:
                        all_predictions.append(new_predictions[uncached_timestamps.index(ts)])
                
                return all_predictions
            except Exception as fallback_error:
                logger.error(f"Fallback to CatBoost also failed: {fallback_error}")
        
        raise HTTPException(status_code=500, detail=str(e))

@api_router.post("/scale")
async def scale_endpoint(request: ScalingRequest):
    """Scale AWS EC2 Auto Scaling Group"""
    try:
        result = scale_ec2_instances(request.predicted_load, request.asg_name)
        return result
    except Exception as e:
        logger.error(f"Scaling error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class ReasonRequest(BaseModel):
    prompt: str | None = None
    context: Dict[str, Any] | None = None


@api_router.post("/reason")
async def reason_endpoint(request: ReasonRequest):
    """Return reasoning for a recommendation. If GEMINI_API_KEY and GEMINI_API_URL
    are set, proxy the prompt to that endpoint. Otherwise return a safe local
    explanation built from the provided context.
    """
    gemini_key = os.environ.get('GEMINI_API_KEY', '')
    gemini_url = os.environ.get('GEMINI_API_URL', GEMINI_API_URL)

    # Build prompt when not provided
    prompt = request.prompt
    ctx = request.context or {}
    if not prompt:
        prompt = (
            f"Explain the autoscaling recommendation.\n"
            f"Model: {ctx.get('model','unknown')}\n"
            f"Peak load: {ctx.get('peakLoad', ctx.get('peak_load','unknown'))}\n"
            f"Recommended instances: {ctx.get('recommendedInstances', ctx.get('recommended_instances','unknown'))}\n"
            f"Date: {ctx.get('date','unknown')}\n"
            "Provide a concise, actionable explanation and safety considerations."
        )

    # Try remote Gemini service first
    if gemini_key and gemini_url:
        try:
            headers = {
                'Content-Type': 'application/json'
            }
            payload = { 'input': prompt }
            resp = requests.post(f"{gemini_url}?key={gemini_key}", headers=headers, json=payload, timeout=20)
            resp.raise_for_status()
            try:
                body = resp.json()
            except Exception:
                body = { 'raw': resp.text }

            return {
                'mode': 'remote',
                'provider_response': body
            }
        except Exception as e:
            logger.warning(f"Gemini proxy failed: {e}, falling back to local explanation")

    # Local deterministic fallback
    try:
        peak = ctx.get('peakLoad') or ctx.get('peak_load') or 'unknown'
        rec = ctx.get('recommendedInstances') or ctx.get('recommended_instances') or 'unknown'
        model = ctx.get('model', 'the configured model')

        explanation = (
            f"Recommendation summary:\n"
            f"- Based on {model}, the predicted peak load is {peak}.\n"
            f"- The autoscaler recommends {rec} instance(s) to meet expected demand while maintaining safety margins.\n"
            "Why:\n"
            "1) The predicted request rate requires capacity to avoid increased latency and errors.\n"
            "2) The autoscaler enforces cooldowns and min/max caps to prevent flapping.\n"
            "Actions:\n"
            "- Approve the scale action and monitor metrics for 15–30 minutes.\n"
            "- If the spike is unexpected, investigate traffic sources and roll back if necessary."
        )

        return { 'mode': 'local', 'explanation': explanation }
    except Exception as e:
        logger.exception('Failed to build explanation')
        raise HTTPException(status_code=500, detail=str(e))

## NOTE: Specific year routes must be declared before the dynamic date route

@api_router.get("/models")
async def get_models():
    """Get available models"""
    return {
        "models": list(MODELS.keys()) if MODELS else [],
        "default": "catboost"
    }

@api_router.get("/next-festival")
async def get_next_festival(model_name: str = 'catboost'):
    """Get next upcoming festival with predictions"""
    try:
        today = datetime.now()
        
        # Look for next festival within next 60 days
        for days_ahead in range(1, 61):
            check_date = today + timedelta(days=days_ahead)
            date_str = check_date.strftime('%Y-%m-%d')
            
            festival_info = check_festival_calendarific(date_str)
            
            if festival_info['is_festival'] == 1:
                # Found next festival, get 24h predictions
                predictions = []
                # Check if model has already failed - switch early to avoid 24 failures
                actual_model = model_name
                if model_name != 'catboost' and model_name in MODEL_FAILURE_COUNT:
                    if MODEL_FAILURE_COUNT[model_name] >= MAX_FAILURES:
                        logger.info(f"Model {model_name} has failed {MODEL_FAILURE_COUNT[model_name]} times previously, using CatBoost")
                        actual_model = 'catboost'
                
                for hour in range(24):
                    timestamp = check_date.replace(hour=hour, minute=0, second=0, microsecond=0)
                    try:
                        pred = predict_traffic(timestamp, actual_model, use_cache=True)
                        predictions.append(pred)
                    except Exception as e:
                        logger.warning(f"Error predicting hour {hour} with {actual_model}: {e}")
                        # Final fallback to CatBoost if still failing
                        if actual_model != 'catboost' and 'catboost' in MODELS:
                            pred = predict_traffic(timestamp, 'catboost', use_cache=True)
                            predictions.append(pred)
                            # Switch to CatBoost for remaining predictions
                            actual_model = 'catboost'
                        else:
                            # If CatBoost also fails, use default estimate
                            logger.error(f"All models failed, using default estimate")
                            predictions.append({
                                'timestamp': timestamp.isoformat(),
                                'hour': hour,
                                'predicted_load': 1000.0,
                                'is_festival': 1,
                                'festival_name': festival_info['festival_name'],
                                'boost': festival_info.get('boost', 1.0),
                                'model': 'fallback'
                            })
                
                # Calculate metrics
                loads = [p['predicted_load'] for p in predictions]
                avg_load = sum(loads) / len(loads)
                peak_load = max(loads)
                
                # Recommended instances based on peak load (updated logic)
                if peak_load < 700:
                    recommended_instances = 1
                elif peak_load < 1400:
                    recommended_instances = 2
                elif peak_load < 2100:
                    recommended_instances = 3
                elif peak_load < 3000:
                    recommended_instances = 4
                elif peak_load < 5000:
                    recommended_instances = 5
                else:
                    recommended_instances = 10
                
                return {
                    'festival_name': festival_info['festival_name'],
                    'date': date_str,
                    'days_until': days_ahead,
                    'avg_load': round(avg_load),
                    'peak_load': round(peak_load),
                    'recommended_instances': recommended_instances,
                    'predictions': predictions
                }
        
        # No festival found in next 60 days
        return {
            'festival_name': None,
            'date': None,
            'days_until': None,
            'avg_load': 0,
            'peak_load': 0,
            'recommended_instances': 0,
            'predictions': []
        }
        
    except Exception as e:
        logger.error(f"Next festival error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/festivals/2025")
async def get_2025_festivals(model_name: str = 'catboost', include_predictions: bool = False, summary_only: bool = True):
    """Get all 2025 major festivals with predictions and traffic spikes
    
    Args:
        model_name: ML model to use (catboost, lightgbm, xgboost)
        include_predictions: If True, includes 24-hour predictions (slower). Default: False for faster response.
    """
    try:
        festivals_with_predictions = []
        
        # Get all 2025 festivals from INDIAN_FESTIVALS
        for date_str, festival_data in INDIAN_FESTIVALS.items():
            if date_str.startswith('2025'):
                festival_name = festival_data['name']
                boost = festival_data['boost']
                
                # Parse date
                festival_date = datetime.strptime(date_str, '%Y-%m-%d')
                
                # Get predictions - only if requested (for performance)
                predictions = []
                if include_predictions and not summary_only:
                    # Get 24-hour predictions for this festival
                    for hour in range(24):
                        timestamp = festival_date.replace(hour=hour, minute=0, second=0, microsecond=0)
                        try:
                            pred = predict_traffic(timestamp, model_name)
                            predictions.append(pred)
                        except Exception as e:
                            logger.warning(f"Error predicting for {date_str} hour {hour}: {e}")
                            # Fallback: use CatBoost if other model fails
                            if model_name != 'catboost':
                                pred = predict_traffic(timestamp, 'catboost')
                                predictions.append(pred)
                else:
                    # Quick/Summary mode: predict peak hour (12:00) for metrics
                    peak_timestamp = festival_date.replace(hour=12, minute=0, second=0, microsecond=0)
                    avg_load, peak_load = get_peak_prediction_with_fallback(peak_timestamp, model_name, boost)
                
                # Calculate metrics
                if include_predictions and predictions:
                    loads = [p['predicted_load'] for p in predictions]
                    avg_load = sum(loads) / len(loads)
                    peak_load = max(loads)
                    peak_hour = predictions[loads.index(peak_load)]['hour']
                else:
                    # Use estimated values from quick mode (already calculated above)
                    peak_hour = 12
                
                # Recommended instances
                recommended_instances = calculate_recommended_instances(peak_load)
                
                # Try to find previous year same festival for comparison (only if predictions requested)
                previous_year_data = None
                if include_predictions:
                    date_prev = date_str.replace('2025', '2024')
                    if date_prev in INDIAN_FESTIVALS and INDIAN_FESTIVALS[date_prev]['name'] == festival_name:
                        # Get previous year predictions (as historical data) - just peak hour for speed
                        prev_date = datetime.strptime(date_prev, '%Y-%m-%d')
                        prev_peak_timestamp = prev_date.replace(hour=12, minute=0, second=0, microsecond=0)
                        try:
                            prev_peak_pred = predict_traffic(prev_peak_timestamp, model_name, use_cache=True)
                            prev_avg_load = prev_peak_pred['predicted_load'] * 0.85
                            prev_peak_load = prev_peak_pred['predicted_load'] * 1.15
                        except Exception as e:
                            logger.warning(f"Error predicting {date_prev}: {e}")
                            prev_avg_load = avg_load * 0.9  # Estimate
                            prev_peak_load = peak_load * 0.9
                        
                        previous_year_data = {
                            'date': date_prev,
                            'avg_load': round(prev_avg_load),
                            'peak_load': round(prev_peak_load),
                            'growth_rate': round(((avg_load - prev_avg_load) / prev_avg_load) * 100, 2) if prev_avg_load > 0 else 0
                        }
                
                festival_data = {
                    'festival_name': festival_name,
                    'date': date_str,
                    'day_of_week': festival_date.strftime('%A'),
                    'month': festival_date.strftime('%B'),
                    'boost': boost,
                    'avg_load': round(avg_load),
                    'peak_load': round(peak_load),
                    'peak_hour': peak_hour,
                    'recommended_instances': recommended_instances,
                }
                
                # Only include predictions and previous_year if requested
                if include_predictions and not summary_only:
                    festival_data['previous_year'] = previous_year_data
                    festival_data['predictions'] = predictions
                
                festivals_with_predictions.append(festival_data)
        
        # Sort by date
        festivals_with_predictions.sort(key=lambda x: x['date'])
        
        return {
            'year': 2025,
            'total_festivals': len(festivals_with_predictions),
            'festivals': festivals_with_predictions
        }
        
    except Exception as e:
        logger.error(f"2025 festivals error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/festivals/2026")
async def get_2026_festivals(model_name: str = 'catboost', include_predictions: bool = False, summary_only: bool = True):
    """Get all 2026 major festivals with predictions and traffic spikes
    
    Args:
        model_name: ML model to use (catboost, lightgbm, xgboost)
        include_predictions: If True, includes 24-hour predictions (slower). Default: False for faster response.
        summary_only: If True, uses boost multipliers only (NO predictions, fastest). Default: True.
    """
    try:
        festivals_with_predictions = []
        
        # Check if model has failed too many times - auto-fallback to CatBoost
        actual_model = model_name
        if model_name != 'catboost' and model_name in MODEL_FAILURE_COUNT:
            if MODEL_FAILURE_COUNT[model_name] >= MAX_FAILURES:
                logger.info(f"Model {model_name} has failed {MODEL_FAILURE_COUNT[model_name]} times previously, using CatBoost")
                actual_model = 'catboost'
        
        # Get all 2026 festivals from INDIAN_FESTIVALS
        for date_str, festival_data in INDIAN_FESTIVALS.items():
            if date_str.startswith('2026'):
                festival_name = festival_data['name']
                boost = festival_data['boost']
                
                # Parse date
                festival_date = datetime.strptime(date_str, '%Y-%m-%d')
                
                # Get predictions - only if requested (for performance)
                predictions = []
                if include_predictions and not summary_only:
                    # Get 24-hour predictions for this festival
                    for hour in range(24):
                        timestamp = festival_date.replace(hour=hour, minute=0, second=0, microsecond=0)
                        try:
                            pred = predict_traffic(timestamp, actual_model, use_cache=True)
                            predictions.append(pred)
                        except Exception as e:
                            logger.warning(f"Error predicting for {date_str} hour {hour}: {e}")
                            # Fallback: use CatBoost if other model fails
                            if actual_model != 'catboost':
                                pred = predict_traffic(timestamp, 'catboost', use_cache=True)
                                predictions.append(pred)
                                # Switch to CatBoost for remaining predictions
                                actual_model = 'catboost'
                            else:
                                # If CatBoost also fails, use default estimate
                                logger.error(f"All models failed, using default estimate")
                                predictions.append({
                                    'timestamp': timestamp.isoformat(),
                                    'hour': hour,
                                    'predicted_load': 1000.0 * boost,
                                    'is_festival': 1,
                                    'festival_name': festival_name,
                                    'boost': boost,
                                    'model': 'fallback'
                                })
                else:
                    # Quick/Summary mode: predict peak hour (12:00) for metrics
                    peak_timestamp = festival_date.replace(hour=12, minute=0, second=0, microsecond=0)
                    avg_load, peak_load = get_peak_prediction_with_fallback(peak_timestamp, actual_model, boost)
                
                # Calculate metrics
                if include_predictions and predictions:
                    loads = [p['predicted_load'] for p in predictions]
                    avg_load = sum(loads) / len(loads)
                    peak_load = max(loads)
                    peak_hour = predictions[loads.index(peak_load)]['hour']
                else:
                    # Use estimated values from quick mode (already calculated above)
                    peak_hour = 12
                
                # Recommended instances
                recommended_instances = calculate_recommended_instances(peak_load)
                
                # Try to find previous year same festival for comparison (only if predictions requested)
                previous_year_data = None
                if include_predictions:
                    date_prev = date_str.replace('2026', '2025')
                    if date_prev in INDIAN_FESTIVALS and INDIAN_FESTIVALS[date_prev]['name'] == festival_name:
                        # Get previous year predictions (as historical data) - just peak hour for speed
                        prev_date = datetime.strptime(date_prev, '%Y-%m-%d')
                        prev_peak_timestamp = prev_date.replace(hour=12, minute=0, second=0, microsecond=0)
                        try:
                            prev_peak_pred = predict_traffic(prev_peak_timestamp, actual_model, use_cache=True)
                            prev_avg_load = prev_peak_pred['predicted_load'] * 0.85
                            prev_peak_load = prev_peak_pred['predicted_load'] * 1.15
                        except Exception as e:
                            logger.warning(f"Error predicting {date_prev}: {e}")
                            prev_avg_load = avg_load * 0.9  # Estimate
                            prev_peak_load = peak_load * 0.9
                        
                        previous_year_data = {
                            'date': date_prev,
                            'avg_load': round(prev_avg_load),
                            'peak_load': round(prev_peak_load),
                            'growth_rate': round(((avg_load - prev_avg_load) / prev_avg_load) * 100, 2) if prev_avg_load > 0 else 0
                        }
                
                festival_data = {
                    'festival_name': festival_name,
                    'date': date_str,
                    'day_of_week': festival_date.strftime('%A'),
                    'month': festival_date.strftime('%B'),
                    'boost': boost,
                    'avg_load': round(avg_load),
                    'peak_load': round(peak_load),
                    'peak_hour': peak_hour,
                    'recommended_instances': recommended_instances,
                }
                
                # Only include predictions and previous_year if requested
                if include_predictions and not summary_only:
                    festival_data['previous_year'] = previous_year_data
                    festival_data['predictions'] = predictions
                
                festivals_with_predictions.append(festival_data)
        
        # Sort by date
        festivals_with_predictions.sort(key=lambda x: x['date'])
        
        return {
            'year': 2026,
            'total_festivals': len(festivals_with_predictions),
            'festivals': festivals_with_predictions
        }
        
    except Exception as e:
        logger.error(f"2026 festivals error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/festivals/{date}")
async def check_festival(date: str):
    """Check if date is a festival"""
    try:
        # Validate date format - must be YYYY-MM-DD
        if len(date) == 4 and date.isdigit():
            # If only year provided, return error or handle differently
            raise HTTPException(status_code=400, detail="Date must be in format YYYY-MM-DD, not just year")
        
        # Try to parse the date to validate format
        try:
            datetime.strptime(date, '%Y-%m-%d')
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid date format: {date}. Expected YYYY-MM-DD")
        
        festival_info = check_festival_calendarific(date)
        return festival_info
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@api_router.get("/aws/instances")
async def get_aws_instances():
    """Get current AWS EC2 instances in Auto Scaling Group"""
    try:
        client = get_aws_autoscaling_client()
        asg_name = os.environ.get('AWS_ASG_NAME', 'my-web-asg')
        
        if not client:
            return {
                'mode': 'mock',
                'message': 'AWS credentials not configured',
                'instances': []
            }
        
        # Get Auto Scaling Group details
        response = client.describe_auto_scaling_groups(
            AutoScalingGroupNames=[asg_name]
        )
        
        if not response['AutoScalingGroups']:
            return {
                'mode': 'real',
                'error': f'Auto Scaling Group "{asg_name}" not found',
                'instances': []
            }
        
        asg = response['AutoScalingGroups'][0]
        
        # Get instance details
        ec2_client = boto3.client(
            'ec2',
            aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
            region_name=os.environ.get('AWS_REGION', 'us-east-1')
        )
        
        instances = []
        for instance in asg['Instances']:
            instance_id = instance['InstanceId']
            
            # Get more details from EC2
            ec2_response = ec2_client.describe_instances(InstanceIds=[instance_id])
            if ec2_response['Reservations']:
                ec2_instance = ec2_response['Reservations'][0]['Instances'][0]
                
                instances.append({
                    'instance_id': instance_id,
                    'instance_type': ec2_instance.get('InstanceType', 'unknown'),
                    'state': ec2_instance['State']['Name'],
                    'health_status': instance['HealthStatus'],
                    'availability_zone': instance['AvailabilityZone'],
                    'launch_time': ec2_instance.get('LaunchTime', '').isoformat() if ec2_instance.get('LaunchTime') else None,
                    'private_ip': ec2_instance.get('PrivateIpAddress', 'N/A'),
                    'public_ip': ec2_instance.get('PublicIpAddress', 'N/A')
                })
        
        return {
            'mode': 'real',
            'asg_name': asg_name,
            'desired_capacity': asg['DesiredCapacity'],
            'min_size': asg['MinSize'],
            'max_size': asg['MaxSize'],
            'current_instances': len(instances),
            'instances': instances
        }
        
    except (ClientError, NoCredentialsError) as e:
        logger.error(f"AWS instances error: {e}")
        return {
            'mode': 'error',
            'error': str(e),
            'instances': []
        }

@api_router.post("/aws/update-instance")
async def update_aws_instance(instance_id: str, action: str):
    """Update specific AWS instance (terminate, stop, start)"""
    try:
        if action not in ['terminate', 'stop', 'start']:
            raise HTTPException(status_code=400, detail="Invalid action. Use 'terminate', 'stop', or 'start'")
        
        aws_access_key = os.environ.get('AWS_ACCESS_KEY_ID')
        aws_secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY')
        
        if not aws_access_key or not aws_secret_key:
            return {
                'mode': 'mock',
                'message': f'[MOCK] Would {action} instance {instance_id}',
                'action': action,
                'instance_id': instance_id
            }
        
        ec2_client = boto3.client(
            'ec2',
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_key,
            region_name=os.environ.get('AWS_REGION', 'us-east-1')
        )
        
        if action == 'terminate':
            ec2_client.terminate_instances(InstanceIds=[instance_id])
            message = f'Instance {instance_id} terminated'
        elif action == 'stop':
            ec2_client.stop_instances(InstanceIds=[instance_id])
            message = f'Instance {instance_id} stopped'
        elif action == 'start':
            ec2_client.start_instances(InstanceIds=[instance_id])
            message = f'Instance {instance_id} started'
        
        return {
            'mode': 'real',
            'success': True,
            'action': action,
            'instance_id': instance_id,
            'message': message
        }
        
    except (ClientError, NoCredentialsError) as e:
        logger.error(f"AWS update instance error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Original routes
@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    """Create a status check (requires MongoDB)"""
    if not MOTOR_AVAILABLE:
        raise HTTPException(
            status_code=503, 
            detail="MongoDB driver (Motor) not available. Install with: pip install 'motor>=3.3.0' 'pymongo>=4.0,<5.0'"
        )
    
    if not db:
        raise HTTPException(
            status_code=503, 
            detail="MongoDB not configured. Please set MONGO_URL in environment variables."
        )
    
    status_dict = input.model_dump()
    status_obj = StatusCheck(**status_dict)
    
    doc = status_obj.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    
    _ = await db.status_checks.insert_one(doc)
    return status_obj

@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    """Get status checks (requires MongoDB)"""
    if not MOTOR_AVAILABLE or not db:
        return []  # Return empty list if MongoDB not available or not configured
    
    status_checks = await db.status_checks.find({}, {"_id": 0}).to_list(1000)
    
    for check in status_checks:
        if isinstance(check['timestamp'], str):
            check['timestamp'] = parse_iso_datetime(check['timestamp'])
    
    return status_checks

# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],  # Allow all origins (for development only)
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown_db_client():
    if client:
        client.close()
