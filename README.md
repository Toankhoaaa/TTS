# Video Processing Backend - Production Ready

Hệ thống xử lý video tự động với:
- Speech-to-Text đa ngôn ngữ (faster-whisper)
- Dịch thuật (Google Translate - deep-translator)
- TTS tiếng Việt (Edge TTS - Microsoft)
- Render video với phụ đề + lồng tiếng

## Kiến trúc

```
TTS/
├── media-service/          # Core processing (STT, Translate, TTS, Render)
├── crawler-service/        # Video download (TikTok, Douyin, YouTube...)
├── job-service/           # Job orchestration
├── docker-compose.yml     # Service orchestration
└── README.md
```

## Quick Start

### Docker Compose (Recommended)

```bash
# Build and run all services
docker-compose up --build

# Services will be available at:
# - Media Service: http://localhost:8001
# - Crawler Service: http://localhost:8002
# - Job Service: http://localhost:8003
```

### Local Development

```bash
# Media Service
cd media-service
pip install -r requirements.txt
python main.py

# Crawler Service
cd crawler-service
pip install -r requirements.txt
python main.py

# Job Service
cd job-service
pip install -r requirements.txt
python main.py
```

## API Endpoints

### Media Service (Port 8001)

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| POST | `/transcribe` | Chuyển audio/video → phụ đề (SRT) |
| POST | `/translate` | Dịch phụ đề → tiếng Việt |
| POST | `/tts` | Tạo giọng đọc từ phụ đề |
| POST | `/render` | Render video cuối |
| POST | `/process` | Full pipeline |
| GET | `/status/{job_id}` | Kiểm tra trạng thái |
| GET | `/health` | Health check |

### Crawler Service (Port 8002)

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| POST | `/download` | Tải video từ URL |
| POST | `/upload` | Upload file local |
| GET | `/health` | Health check |

### Job Service (Port 8003)

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| POST | `/jobs` | Tạo job mới |
| GET | `/jobs/{job_id}` | Lấy thông tin job |
| GET | `/jobs/{job_id}/result` | Lấy video output |
| GET | `/jobs` | List all jobs |
| DELETE | `/jobs/{job_id}` | Xóa job |

## Pipeline xử lý

```
1. Download Video
   └─> crawler-service (TikTok, Douyin, YouTube...)

2. Extract Audio
   └─> ffmpeg

3. Transcribe → SRT
   └─> faster-whisper (medium model)
       - beam_size=5
       - temperature=0
       - best_of=5

4. Translate từng dòng
   └─> Google Translate (deep-translator)
       - KHÔNG rewrite
       - KHÔNG summarize
       - EXACT translation

5. Generate TTS
   └─> Edge TTS (Microsoft)
       - vi-VN-HoaiMyNeural
       - rate=+0% (tự nhiên)
       - KHÔNG speed > 1.1

6. Sync Audio
   └─> ffmpeg
       - Dùng timing gốc
       - KHÔNG ép tốc độ cao

7. Render Video
   └─> ffmpeg
       - Phụ đề hard-coded
       - Mix audio: gốc + voiceover
```

## Usage Examples

### Curl Commands

```bash
# 1. Transcribe video
curl -X POST "http://localhost:8001/transcribe" \
  -F "file=@video.mp4"

# 2. Translate subtitles
curl -X POST "http://localhost:8001/translate" \
  -H "Content-Type: application/json" \
  -d '{"subtitle_path": "/path/to/subtitle.srt", "source_lang": "en"}'

# 3. Generate TTS
curl -X POST "http://localhost:8001/tts" \
  -H "Content-Type: application/json" \
  -d '{"subtitle_path": "/path/to/translated.srt"}'

# 4. Render final video
curl -X POST "http://localhost:8001/render" \
  -H "Content-Type: application/json" \
  -d '{
    "video_path": "/path/to/video.mp4",
    "subtitle_path": "/path/to/subtitle.srt",
    "voiceover_path": "/path/to/voiceover.mp3"
  }'

# 5. Download from URL
curl -X POST "http://localhost:8002/download" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.tiktok.com/@user/video/123"}'
```

### Python Client

```python
import httpx

# Full pipeline
async with httpx.AsyncClient() as client:
    # Download
    dl_resp = await client.post(
        "http://localhost:8002/download",
        json={"url": "https://tiktok.com/..."}
    )
    video_path = dl_resp.json()["video_path"]

    # Process
    resp = await client.post(
        "http://localhost:8001/process",
        files={"file": open(video_path, "rb")}
    )
```

## Requirements

### System
- Python 3.10+
- ffmpeg (system)

### Python Packages (media-service)
```
faster-whisper==1.0.3      # STT
deep-translator==1.11.4     # Translation
edge-tts==6.1.10           # TTS
fastapi==0.109.2           # API
uvicorn==0.27.1           # Server
pillow==10.2.0            # Image processing
```

### Python Packages (crawler-service)
```
yt-dlp==2024.2.5          # Video download
```

## Configuration

### Environment Variables

```bash
# Media Service
MEDIA_DIR=/app/data              # Working directory
WHISPER_MODEL=medium             # Model size
WHISPER_DEVICE=cpu               # cpu/cuda
MAX_CONCURRENT_JOBS=2
LOG_LEVEL=INFO

# Services
MEDIA_SERVICE_URL=http://localhost:8001
CRAWLER_SERVICE_URL=http://localhost:8002
```

## Nguyên tắc quan trọng

### ✅ ĐƯỢC LÀM
- Dịch EXACT từng dòng (KHÔNG rewrite)
- Giữ nguyên timing từ Whisper
- TTS = EXACT subtitle text
- Fallback khi TTS lỗi (dùng audio gốc)

### ❌ KHÔNG ĐƯỢC
- Dùng LLM để dịch
- Rewrite/summarize content
- Ép tốc độ TTS > 1.1
- Bỏ qua segment lỗi (log warning)
- Crash khi subtitle/audio lỗi

## Error Handling

```python
# Tất cả errors đều được log, không crash:
try:
    await process_segment(seg)
except Exception as e:
    logger.warning(f"Segment {i} failed: {e}")
    continue  # Continue with next segment
```

## Cấu trúc thư mục output

```
/app/data/
└── {job_id}/
    ├── video_{job_id}.mp4          # Original video
    ├── audio_{job_id}.mp3          # Extracted audio
    ├── subtitle_{job_id}.srt       # Original subtitle
    ├── translated_subtitle_{job_id}.srt  # Vietnamese subtitle
    ├── voiceover_{job_id}.mp3      # TTS voiceover
    ├── tts_segments/               # Individual TTS files
    └── final_video_{job_id}.mp4    # Final output
```

## Production Deployment

```yaml
# docker-compose.prod.yml
services:
  media-service:
    deploy:
      replicas: 2
      resources:
        limits:
          memory: 4G
    shm_size: 2gb
    volumes:
      - /data/media:/app/data

  crawler-service:
    deploy:
      replicas: 2

  job-service:
    deploy:
      replicas: 2
```

## License

MIT
