import requests
import csv
import re
import io
import json
import threading
import queue
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from flask import Flask, render_template_string, request, jsonify, Response

app = Flask(__name__)

# ---------------- CONFIG ----------------
API_KEY = "AIzaSyC72nVfrLscKiz1fpv1QFOpQ0kyTsP-7qw"

# ---------------- TRANSCRIPT API ----------------
def extract_video_id(url):
    """Extract the video ID from various YouTube URL formats"""
    # Handle youtube.com URLs
    parsed_url = urlparse(url)
    if 'youtube.com' in parsed_url.netloc:
        if '/watch' in parsed_url.path:
            return parse_qs(parsed_url.query).get('v', [None])[0]
        elif '/shorts/' in parsed_url.path:
            return parsed_url.path.split('/shorts/')[1].split('?')[0]
    # Handle youtu.be URLs
    elif 'youtu.be' in parsed_url.netloc:
        return parsed_url.path.lstrip('/')
    return None

def get_transcript(video_url):
    """Get transcript from YouTube video URL"""
    video_id = extract_video_id(video_url)
    if not video_id:
        return "Invalid YouTube URL. Could not extract video ID."
    
    cookies = {
        'CookieConsent': '{stamp:%27-1%27%2Cnecessary:true%2Cpreferences:true%2Cstatistics:true%2Cmarketing:true%2Cmethod:%27implied%27%2Cver:1%2Cutc:1758037169904%2Ciab2:%27%27%2Cregion:%27IN%27}',
    }

    headers = {
        'authority': 'youtubetotranscript.com',
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'accept-language': 'en-US,en;q=0.9',
        'content-type': 'application/x-www-form-urlencoded',
        'origin': 'https://youtubetotranscript.com',
        'referer': 'https://youtubetotranscript.com/',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36',
    }

    data = {
        'youtube_url': video_url,
    }

    try:
        response = requests.post('https://youtubetotranscript.com/transcript', 
                                cookies=cookies, 
                                headers=headers, 
                                data=data,
                                timeout=30)
        response.raise_for_status()
        
        # Parse the HTML response
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Get video title
        title_element = soup.find('h1', class_='card-title')
        title = title_element.text.strip() if title_element else "Title not found"
        
        # Get author
        author_element = soup.find('a', {'data-ph-capture-attribute-element': 'author-link'})
        author = author_element.text.strip() if author_element else "Author not found"
        
        # Extract transcript segments
        transcript_segments = soup.find_all('span', class_='transcript-segment')
        
        if not transcript_segments:
            return {
                "title": title,
                "author": author,
                "transcript": "No transcript found for this video."
            }
        
        # Format the transcript with timestamps
        transcript_text = ""
        
        for segment in transcript_segments:
            start_time = segment.get('data-start')
            if start_time:
                # Convert seconds to MM:SS format
                start_seconds = float(start_time)
                minutes = int(start_seconds // 60)
                seconds = int(start_seconds % 60)
                timestamp = f"[{minutes:02d}:{seconds:02d}]"
                
                # Add the segment text with timestamp
                transcript_text += f"{timestamp} {segment.text.strip()}\n"
            else:
                transcript_text += f"{segment.text.strip()}\n"
        
        return {
            "title": title,
            "author": author,
            "transcript": transcript_text
        }
        
    except requests.exceptions.RequestException as e:
        return {
            "title": "Error",
            "author": "Error",
            "transcript": f"Error retrieving transcript: {str(e)}"
        }

def get_multiple_transcripts(video_urls):
    """Use multithreading to get transcripts for multiple videos"""
    transcripts = {}
    threads = []
    result_queue = queue.Queue()
    
    def worker(url, queue):
        try:
            video_id = extract_video_id(url)
            transcript = get_transcript(url)
            queue.put((video_id, transcript))
        except Exception as e:
            queue.put((extract_video_id(url), {
                "title": "Error",
                "author": "Error",
                "transcript": f"Error: {str(e)}"
            }))
    
    # Create and start threads
    for url in video_urls:
        thread = threading.Thread(target=worker, args=(url, result_queue))
        threads.append(thread)
        thread.start()
    
    # Wait for all threads to complete
    for thread in threads:
        thread.join()
    
    # Collect results
    while not result_queue.empty():
        video_id, transcript = result_queue.get()
        transcripts[video_id] = transcript
    
    return transcripts

# ---------------- HELPERS ----------------
def get_channel_id(url):
    """Extract channel ID from URL or resolve handle"""
    if "channel/" in url:
        return url.split("channel/")[1].split("/")[0]
    elif "user/" in url:
        username = url.split("user/")[1].split("/")[0]
        data = requests.get(
            f"https://www.googleapis.com/youtube/v3/channels?part=id&forUsername={username}&key={API_KEY}"
        ).json()
        if "items" in data and len(data["items"]) > 0:
            return data["items"][0]["id"]
    elif "@" in url:  # handle support
        handle = url.split("@")[1].split("/")[0].split("?")[0]
        search_url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&type=channel&q={handle}&key={API_KEY}"
        data = requests.get(search_url).json()
        if "items" in data and len(data["items"]) > 0:
            return data["items"][0]["snippet"]["channelId"]
    raise ValueError("Invalid YouTube URL provided.")

def fetch_channel_info(channel_id):
    url = f"https://www.googleapis.com/youtube/v3/channels?part=snippet,statistics,brandingSettings,contentDetails,status&id={channel_id}&key={API_KEY}"
    data = requests.get(url).json()
    if "items" not in data:
        return None
    return data["items"][0]

def fetch_videos(channel_id, max_results=100):
    """Fetch uploaded videos, limited to max_results"""
    uploads = requests.get(
        f"https://www.googleapis.com/youtube/v3/channels?part=contentDetails&id={channel_id}&key={API_KEY}"
    ).json()
    
    if "items" not in uploads or len(uploads["items"]) == 0:
        return []
        
    playlist_id = uploads["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    videos = []
    next_page = None
    
    url = f"https://www.googleapis.com/youtube/v3/playlistItems?part=snippet&playlistId={playlist_id}&maxResults={max_results}&key={API_KEY}"
    res = requests.get(url).json()
    
    if "items" not in res:
        return []
    
    video_ids = [item["snippet"]["resourceId"]["videoId"] for item in res.get("items", [])]
    if video_ids:
        # Get details for all videos in a single request
        vdata = requests.get(
            f"https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics,contentDetails,status&id={','.join(video_ids)}&key={API_KEY}"
        ).json()
        
        if "items" in vdata:
            videos.extend(vdata["items"])
    
    return videos

def fetch_shorts(channel_id, max_results=20):
    """Attempt to fetch shorts from the channel"""
    search_url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&channelId={channel_id}&maxResults={max_results}&type=video&videoDuration=short&key={API_KEY}"
    search_data = requests.get(search_url).json()
    
    if "items" not in search_data:
        return []
    
    short_video_ids = [item["id"]["videoId"] for item in search_data.get("items", [])]
    
    if not short_video_ids:
        return []
        
    # Get full details for these videos
    shorts_data = requests.get(
        f"https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics,contentDetails,status&id={','.join(short_video_ids)}&key={API_KEY}"
    ).json()
    
    if "items" not in shorts_data:
        return []
    
    # Filter for actual shorts (typically very short duration)
    shorts = []
    for video in shorts_data["items"]:
        duration = video["contentDetails"]["duration"]
        # Parse duration like "PT2M30S" (ISO 8601 duration format)
        if 'H' not in duration and ('30S' in duration or '20S' in duration or '10S' in duration or '15S' in duration or 'M1' in duration):
            shorts.append(video)
    
    return shorts

def estimate_income(views):
    """Estimate income from views"""
    cpm_low, cpm_high = 0.5, 3.0  # USD per 1000 views
    monthly_est = round((views / 12 / 1000) * cpm_low, 2), round((views / 12 / 1000) * cpm_high, 2)
    lifetime_est = round((views / 1000) * cpm_low, 2), round((views / 1000) * cpm_high, 2)
    return monthly_est, lifetime_est

def extract_email(text):
    """Extract email from description if present"""
    match = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text)
    return match.group(0) if match else "Not Found"

def extract_social_links(text):
    """Extract social media links from text"""
    social_media = {
        'instagram': r'(?:https?:\/\/)?(?:www\.)?instagram\.com\/[a-zA-Z0-9_\.]+\/?',
        'twitter': r'(?:https?:\/\/)?(?:www\.)?(?:twitter\.com|x\.com)\/[a-zA-Z0-9_]+\/?',
        'facebook': r'(?:https?:\/\/)?(?:www\.)?facebook\.com\/[a-zA-Z0-9\.]+\/?',
        'tiktok': r'(?:https?:\/\/)?(?:www\.)?tiktok\.com\/@[a-zA-Z0-9\.]+\/?',
        'linkedin': r'(?:https?:\/\/)?(?:www\.)?linkedin\.com\/in\/[a-zA-Z0-9\-]+\/?',
    }
    
    results = {}
    for platform, pattern in social_media.items():
        matches = re.findall(pattern, text)
        if matches:
            results[platform] = matches
    
    return results

def generate_growth_chart(videos):
    """Generate data for growth charts - 7 days, 30 days, 6 months"""
    try:
        import plotly.graph_objects as go
        import plotly.utils

        videos_with_date = [(v, datetime.strptime(v["snippet"]["publishedAt"], "%Y-%m-%dT%H:%M:%SZ")) 
                             for v in videos]
        videos_with_date.sort(key=lambda x: x[1])
        
        today = datetime.utcnow()
        
        # Define time periods
        periods = {
            '7d': today - timedelta(days=7),
            '30d': today - timedelta(days=30),
            '6m': today - timedelta(days=180)
        }
        
        # Create data series for each period
        charts_data = {}
        
        for period_name, start_date in periods.items():
            period_videos = [v for v, date in videos_with_date if date >= start_date]
            
            if not period_videos:
                charts_data[period_name] = None
                continue
                
            # Group by date
            dates = []
            views = []
            likes = []
            comments = []
            
            # Get unique dates in period
            unique_dates = sorted(list(set(v[1].date() for v in videos_with_date if v[1] >= start_date)))
            
            for date in unique_dates:
                dates.append(date.strftime('%Y-%m-%d'))
                
                # Sum metrics for videos published on this date
                day_views = sum(int(v[0]["statistics"].get("viewCount", 0)) 
                              for v, d in videos_with_date if d.date() == date)
                day_likes = sum(int(v[0]["statistics"].get("likeCount", 0)) 
                              for v, d in videos_with_date if d.date() == date)
                day_comments = sum(int(v[0]["statistics"].get("commentCount", 0)) 
                                 for v, d in videos_with_date if d.date() == date)
                
                views.append(day_views)
                likes.append(day_likes)
                comments.append(day_comments)
            
            # Create the chart
            fig = go.Figure()
            
            fig.add_trace(go.Scatter(
                x=dates,
                y=views,
                mode='lines+markers',
                name='Views',
                line=dict(color='#FF0000', width=2)
            ))
            
            fig.add_trace(go.Scatter(
                x=dates,
                y=likes,
                mode='lines+markers',
                name='Likes',
                line=dict(color='#4285F4', width=2)
            ))
            
            fig.add_trace(go.Scatter(
                x=dates,
                y=comments,
                mode='lines+markers',
                name='Comments',
                line=dict(color='#34A853', width=2)
            ))
            
            fig.update_layout(
                title=f'Last {period_name.replace("d", " Days").replace("m", " Months")} Performance',
                xaxis_title='Date',
                yaxis_title='Count',
                template='plotly_white',
                legend=dict(
                    orientation="h",
                    yanchor="bottom",
                    y=1.02,
                    xanchor="right",
                    x=1
                )
            )
            
            charts_data[period_name] = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
        
        return charts_data
    except ImportError:
        # If plotly is not available, return empty data
        return {'7d': None, '30d': None, '6m': None}

def generate_csv(channel_info, videos):
    """Generate CSV content as string"""
    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow(["Channel Report"])
    writer.writerow(["Channel Title", channel_info["snippet"]["title"]])
    writer.writerow(["Description", channel_info["snippet"].get("description", "N/A")])
    writer.writerow(["Subscribers", channel_info["statistics"].get("subscriberCount", "N/A")])
    writer.writerow(["Views", channel_info["statistics"].get("viewCount", "N/A")])
    writer.writerow(["Video Count", channel_info["statistics"].get("videoCount", "N/A")])
    writer.writerow(["Joined", channel_info["snippet"]["publishedAt"].split("T")[0]])
    writer.writerow(["Country", channel_info["snippet"].get("country", "N/A")])
    writer.writerow([])
    writer.writerow(["Videos"])
    writer.writerow([
        "Video ID", "Title", "Type", "Published At", "Views", "Likes",
        "Comments", "Duration", "Tags", "Thumbnail"
    ])
    
    for v in videos:
        s, snip, stats = v["id"], v["snippet"], v["statistics"]
        # Determine if it's likely a short
        is_short = False
        if "contentDetails" in v and "duration" in v["contentDetails"]:
            duration = v["contentDetails"]["duration"]
            if 'H' not in duration and ('30S' in duration or '20S' in duration or '10S' in duration or '15S' in duration or 'M1' in duration):
                is_short = True
        
        video_type = "Short" if is_short else "Regular"
        
        writer.writerow([
            s,
            snip["title"],
            video_type,
            snip.get("publishedAt", ""),
            stats.get("viewCount", 0),
            stats.get("likeCount", 0),
            stats.get("commentCount", 0),
            v["contentDetails"]["duration"],
            ", ".join(snip.get("tags", [])) if "tags" in snip else "N/A",
            snip["thumbnails"]["high"]["url"] if "thumbnails" in snip and "high" in snip["thumbnails"] else "N/A"
        ])
    
    return output.getvalue()

# ---------------- HTML TEMPLATES ----------------
# Base HTML template with CSS and JavaScript included
BASE_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Influenca Daren - YouTube Analytics</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.1.1/css/all.min.css">
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>
        :root {
            --primary-color: #ff3e3e;
            --secondary-color: #333333;
            --accent-color: #4e54c8;
            --light-color: #f8f9fa;
            --dark-color: #212529;
        }
        
        body {
            font-family: 'Poppins', sans-serif;
            background-color: var(--light-color);
            color: var(--dark-color);
        }
        
        .navbar {
            background-color: var(--primary-color);
        }
        
        .navbar-brand {
            font-weight: bold;
            color: white !important;
        }
        
        .btn-primary {
            background-color: var(--primary-color);
            border-color: var(--primary-color);
        }
        
        .btn-primary:hover {
            background-color: #e62e2e;
            border-color: #e62e2e;
        }
        
        .btn-outline-primary {
            color: var(--primary-color);
            border-color: var(--primary-color);
        }
        
        .btn-outline-primary:hover {
            background-color: var(--primary-color);
            color: white;
        }
        
        .card {
            border-radius: 10px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            transition: transform 0.3s ease;
            margin-bottom: 20px;
        }
        
        .card:hover {
            transform: translateY(-5px);
        }
        
        .card-header {
            background-color: var(--accent-color);
            color: white;
            border-radius: 10px 10px 0 0 !important;
        }
        
        .loading-overlay {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(255, 255, 255, 0.9);
            display: flex;
            justify-content: center;
            align-items: center;
            flex-direction: column;
            z-index: 9999;
        }
        
        .spinner {
            width: 60px;
            height: 60px;
            border: 5px solid var(--primary-color);
            border-left-color: transparent;
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }
        
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        
        .hero-section {
            background: linear-gradient(135deg, var(--primary-color) 0%, var(--accent-color) 100%);
            color: white;
            padding: 100px 0;
            border-radius: 0 0 50px 50px;
            margin-bottom: 50px;
        }
        
        .stats-card {
            border-left: 5px solid var(--primary-color);
        }
        
        .social-icon {
            font-size: 1.5rem;
            margin-right: 10px;
            color: var(--accent-color);
        }
        
        .video-card {
            margin-bottom: 20px;
        }
        
        .video-card img {
            border-radius: 10px;
        }
        
        .chart-container {
            height: 400px;
            margin-bottom: 30px;
        }
        
        .tab-content {
            padding: 20px;
            background-color: white;
            border-radius: 0 0 10px 10px;
        }
        
        .nav-tabs .nav-link.active {
            background-color: var(--accent-color);
            color: white;
            border: none;
        }
        
        .nav-tabs .nav-link {
            color: var(--dark-color);
        }
        
        .profile-img {
            border-radius: 50%;
            border: 4px solid white;
            width: 120px;
            height: 120px;
            object-fit: cover;
        }
        
        .banner-img {
            width: 100%;
            height: 150px;
            object-fit: cover;
            border-radius: 10px;
        }
        
        .feature-icon {
            font-size: 3rem;
            color: var(--primary-color);
        }
        
        .feature-card {
            text-align: center;
            padding: 30px 20px;
        }
        
        .transcript-text {
            font-family: monospace;
            white-space: pre-line;
            max-height: 500px;
            overflow-y: auto;
            padding: 15px;
            background-color: #f8f9fa;
            border-radius: 5px;
        }
        
        .short-badge {
            background-color: #FF0000;
            color: white;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.75rem;
            margin-left: 5px;
        }
        
        .video-stats {
            display: flex;
            gap: 10px;
            margin-top: 5px;
            font-size: 0.85rem;
            color: #6c757d;
        }
        
        .transcript-header {
            border-bottom: 1px solid #dee2e6;
            padding-bottom: 10px;
            margin-bottom: 15px;
        }
        
        .video-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 20px;
        }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-dark">
        <div class="container">
            <a class="navbar-brand" href="/">Influenca Daren</a>
            <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
                <span class="navbar-toggler-icon"></span>
            </button>
            <div class="collapse navbar-collapse" id="navbarNav">
                <ul class="navbar-nav ms-auto">
                    <li class="nav-item">
                        <a class="nav-link" href="/">Home</a>
                    </li>
                </ul>
            </div>
        </div>
    </nav>

    <div id="loading-overlay" class="loading-overlay d-none">
        <div class="spinner"></div>
        <p class="mt-3">Analyzing YouTube channel data...</p>
    </div>

    <main>
        {{ content | safe }}
    </main>

    <footer class="bg-dark text-white py-4 mt-5">
        <div class="container text-center">
            <p>&copy; 2023 Influenca Daren. All rights reserved.</p>
        </div>
    </footer>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        function showLoading() {
            document.getElementById('loading-overlay').classList.remove('d-none');
        }
        
        function hideLoading() {
            document.getElementById('loading-overlay').classList.add('d-none');
        }
        
        function getTranscript(videoId) {
            showLoading();
            window.location.href = `/transcript/${videoId}`;
        }
    </script>
</body>
</html>
'''

# Index page HTML
INDEX_HTML = '''
<section class="hero-section text-center">
    <div class="container">
        <h1 class="display-4 fw-bold mb-4">Influencers Dream Achiever</h1>
        <p class="lead mb-5">Analyze any YouTube channel instantly and unlock valuable insights to grow your influence!</p>
        <a href="#analyze-section" class="btn btn-light btn-lg px-4 me-2">Get Started</a>
    </div>
</section>

<section id="analyze-section" class="container my-5">
    <div class="row justify-content-center">
        <div class="col-md-8">
            <div class="card">
                <div class="card-header py-3">
                    <h3 class="mb-0 text-center">Analyze YouTube Channel</h3>
                </div>
                <div class="card-body p-4">
                    <form id="channel-form" action="/analyze" method="post" onsubmit="showLoading()">
                        <div class="mb-4">
                            <label for="channel_url" class="form-label">Enter YouTube Channel URL</label>
                            <input type="text" class="form-control form-control-lg" id="channel_url" name="channel_url" 
                                placeholder="https://www.youtube.com/@username or channel URL" required>
                            <div class="form-text">
                                Examples: https://www.youtube.com/@MrBeast, https://www.youtube.com/channel/UC...
                            </div>
                        </div>
                        <div class="d-grid">
                            <button type="submit" class="btn btn-primary btn-lg">Analyze Channel</button>
                        </div>
                    </form>
                </div>
            </div>
        </div>
    </div>
</section>

<section class="container my-5">
    <h2 class="text-center mb-5">What You'll Discover</h2>
    <div class="row g-4">
        <div class="col-md-4">
            <div class="card feature-card h-100">
                <div class="card-body">
                    <i class="fas fa-chart-line feature-icon mb-3"></i>
                    <h4>Growth Analysis</h4>
                    <p>Track channel performance over 7 days, 30 days, and 6 months to identify growth trends.</p>
                </div>
            </div>
        </div>
        <div class="col-md-4">
            <div class="card feature-card h-100">
                <div class="card-body">
                    <i class="fas fa-dollar-sign feature-icon mb-3"></i>
                    <h4>Income Estimation</h4>
                    <p>Get estimated earnings based on views and industry standards.</p>
                </div>
            </div>
        </div>
        <div class="col-md-4">
            <div class="card feature-card h-100">
                <div class="card-body">
                    <i class="fas fa-file-alt feature-icon mb-3"></i>
                    <h4>Video Transcripts</h4>
                    <p>Access complete transcripts for any video to analyze content strategies.</p>
                </div>
            </div>
        </div>
    </div>
</section>
'''

# Results page HTML template
RESULT_HTML = '''
<div class="container mt-5">
    <div class="row">
        <div class="col-md-4 text-center">
            <img src="{{ channel.thumbnail }}" alt="{{ channel.title }}" class="profile-img mb-3">
            <h2>{{ channel.title }}</h2>
            {% if channel.country != 'N/A' %}
            <p><i class="fas fa-map-marker-alt"></i> {{ channel.country }}</p>
            {% endif %}
            <p><i class="fas fa-calendar-alt"></i> Joined: {{ channel.created.split('T')[0] }}</p>
            <div class="mt-3">
                <a href="https://www.youtube.com/channel/{{ channel.id }}" target="_blank" class="btn btn-danger mb-2">
                    <i class="fab fa-youtube"></i> Visit Channel
                </a>
                <a href="/download-csv/{{ channel.id }}" class="btn btn-outline-primary mb-2">
                    <i class="fas fa-download"></i> Download CSV Report
                </a>
            </div>
        </div>
        
        <div class="col-md-8">
            {% if channel.banner %}
            <img src="{{ channel.banner }}" alt="Channel Banner" class="banner-img mb-4">
            {% endif %}
            
            <div class="row">
                <div class="col-md-3">
                    <div class="card stats-card text-center h-100">
                        <div class="card-body">
                            <h6 class="card-subtitle mb-2 text-muted">Subscribers</h6>
                            <h3 class="card-title">{{ channel.subscribers }}</h3>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card stats-card text-center h-100">
                        <div class="card-body">
                            <h6 class="card-subtitle mb-2 text-muted">Total Views</h6>
                            <h3 class="card-title">{{ channel.views }}</h3>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card stats-card text-center h-100">
                        <div class="card-body">
                            <h6 class="card-subtitle mb-2 text-muted">Videos</h6>
                            <h3 class="card-title">{{ channel.videoCount }}</h3>
                        </div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card stats-card text-center h-100">
                        <div class="card-body">
                            <h6 class="card-subtitle mb-2 text-muted">Shorts</h6>
                            <h3 class="card-title">{{ channel.shortsCount }}</h3>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <div class="row mt-5">
        <div class="col-12">
            <div class="card">
                <div class="card-header">
                    <h4>Channel Description</h4>
                </div>
                <div class="card-body">
                    <p style="white-space: pre-line;">{{ channel.description }}</p>
                </div>
            </div>
        </div>
    </div>
    
    <div class="row mt-4">
        <div class="col-md-6">
            <div class="card h-100">
                <div class="card-header">
                    <h4>Estimated Income</h4>
                </div>
                <div class="card-body">
                    <div class="row">
                        <div class="col-md-6">
                            <h5 class="text-muted">Monthly</h5>
                            <h3>{{ channel.monthlyIncome }}</h3>
                        </div>
                        <div class="col-md-6">
                            <h5 class="text-muted">Lifetime</h5>
                            <h3>{{ channel.lifetimeIncome }}</h3>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="col-md-6">
            <div class="card h-100">
                <div class="card-header">
                    <h4>Contact Information</h4>
                </div>
                <div class="card-body">
                    <p><i class="fas fa-envelope social-icon"></i> {{ channel.email }}</p>
                    
                    {% if channel.socialLinks %}
                        <h5 class="mt-3">Social Media</h5>
                        {% for platform, links in channel.socialLinks.items() %}
                            {% for link in links %}
                                <a href="{{ link }}" target="_blank" class="btn btn-sm btn-outline-dark me-2 mb-2">
                                    <i class="fab fa-{{ platform }} social-icon" style="font-size: 1rem;"></i> {{ platform.capitalize() }}
                                </a>
                            {% endfor %}
                        {% endfor %}
                    {% endif %}
                </div>
            </div>
        </div>
    </div>
    
    <!-- Performance Charts -->
    <div class="row mt-4">
        <div class="col-12">
            <div class="card">
                <div class="card-header">
                    <h4>Performance Analysis</h4>
                </div>
                <div class="card-body">
                    <ul class="nav nav-tabs" id="performanceTabs" role="tablist">
                        <li class="nav-item" role="presentation">
                            <button class="nav-link active" id="week-tab" data-bs-toggle="tab" data-bs-target="#week" type="button">Last 7 Days</button>
                        </li>
                        <li class="nav-item" role="presentation">
                            <button class="nav-link" id="month-tab" data-bs-toggle="tab" data-bs-target="#month" type="button">Last 30 Days</button>
                        </li>
                        <li class="nav-item" role="presentation">
                            <button class="nav-link" id="sixmonth-tab" data-bs-toggle="tab" data-bs-target="#sixmonth" type="button">Last 6 Months</button>
                        </li>
                    </ul>
                    <div class="tab-content" id="performanceTabContent">
                        <div class="tab-pane fade show active" id="week" role="tabpanel" aria-labelledby="week-tab">
                            {% if channel.charts['7d'] %}
                                <div id="chart-7d" class="chart-container"></div>
                            {% else %}
                                <div class="alert alert-info">Not enough data available for this time period.</div>
                            {% endif %}
                        </div>
                        <div class="tab-pane fade" id="month" role="tabpanel" aria-labelledby="month-tab">
                            {% if channel.charts['30d'] %}
                                <div id="chart-30d" class="chart-container"></div>
                            {% else %}
                                <div class="alert alert-info">Not enough data available for this time period.</div>
                            {% endif %}
                        </div>
                        <div class="tab-pane fade" id="sixmonth" role="tabpanel" aria-labelledby="sixmonth-tab">
                            {% if channel.charts['6m'] %}
                                <div id="chart-6m" class="chart-container"></div>
                            {% else %}
                                <div class="alert alert-info">Not enough data available for this time period.</div>
                            {% endif %}
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Latest Videos -->
    <div class="row mt-4">
        <div class="col-12">
            <div class="card">
                <div class="card-header d-flex justify-content-between align-items-center">
                    <h4 class="mb-0">Latest Videos</h4>
                    <ul class="nav nav-pills">
                        <li class="nav-item">
                            <button class="nav-link active" id="all-videos-tab" data-bs-toggle="pill" data-bs-target="#all-videos">All</button>
                        </li>
                        <li class="nav-item">
                            <button class="nav-link" id="regular-videos-tab" data-bs-toggle="pill" data-bs-target="#regular-videos">Regular</button>
                        </li>
                        <li class="nav-item">
                            <button class="nav-link" id="shorts-tab" data-bs-toggle="pill" data-bs-target="#shorts">Shorts</button>
                        </li>
                    </ul>
                </div>
                <div class="card-body">
                    <div class="tab-content">
                        <div class="tab-pane fade show active" id="all-videos">
                            <div class="video-grid">
                                {% for video in channel.videos %}
                                <div class="card">
                                    <img src="{{ video.thumbnail }}" class="card-img-top" alt="{{ video.title }}">
                                    <div class="card-body">
                                        <h5 class="card-title">
                                            {{ video.title }}
                                            {% if video.isShort %}
                                            <span class="short-badge">SHORT</span>
                                            {% endif %}
                                        </h5>
                                        <p class="card-text text-muted">{{ video.published.split('T')[0] }}</p>
                                        <div class="video-stats">
                                            <span><i class="far fa-eye"></i> {{ video.views }}</span>
                                            <span><i class="far fa-thumbs-up"></i> {{ video.likes }}</span>
                                            <span><i class="far fa-comment"></i> {{ video.comments }}</span>
                                        </div>
                                        <div class="d-flex mt-3">
                                            <a href="https://www.youtube.com/watch?v={{ video.id }}" target="_blank" class="btn btn-sm btn-outline-danger me-2">
                                                Watch
                                            </a>
                                            <button onclick="getTranscript('{{ video.id }}')" class="btn btn-sm btn-outline-primary">
                                                Get Transcript
                                            </button>
                                        </div>
                                    </div>
                                </div>
                                {% endfor %}
                            </div>
                        </div>
                        
                        <div class="tab-pane fade" id="regular-videos">
                            <div class="video-grid">
                                {% for video in channel.videos %}
                                {% if not video.isShort %}
                                <div class="card">
                                    <img src="{{ video.thumbnail }}" class="card-img-top" alt="{{ video.title }}">
                                    <div class="card-body">
                                        <h5 class="card-title">{{ video.title }}</h5>
                                        <p class="card-text text-muted">{{ video.published.split('T')[0] }}</p>
                                        <div class="video-stats">
                                            <span><i class="far fa-eye"></i> {{ video.views }}</span>
                                            <span><i class="far fa-thumbs-up"></i> {{ video.likes }}</span>
                                            <span><i class="far fa-comment"></i> {{ video.comments }}</span>
                                        </div>
                                        <div class="d-flex mt-3">
                                            <a href="https://www.youtube.com/watch?v={{ video.id }}" target="_blank" class="btn btn-sm btn-outline-danger me-2">
                                                Watch
                                            </a>
                                            <button onclick="getTranscript('{{ video.id }}')" class="btn btn-sm btn-outline-primary">
                                                Get Transcript
                                            </button>
                                        </div>
                                    </div>
                                </div>
                                {% endif %}
                                {% endfor %}
                            </div>
                        </div>
                        
                        <div class="tab-pane fade" id="shorts">
                            <div class="video-grid">
                                {% for video in channel.videos %}
                                {% if video.isShort %}
                                <div class="card">
                                    <img src="{{ video.thumbnail }}" class="card-img-top" alt="{{ video.title }}">
                                    <div class="card-body">
                                        <h5 class="card-title">
                                            {{ video.title }}
                                            <span class="short-badge">SHORT</span>
                                        </h5>
                                        <p class="card-text text-muted">{{ video.published.split('T')[0] }}</p>
                                        <div class="video-stats">
                                            <span><i class="far fa-eye"></i> {{ video.views }}</span>
                                            <span><i class="far fa-thumbs-up"></i> {{ video.likes }}</span>
                                            <span><i class="far fa-comment"></i> {{ video.comments }}</span>
                                        </div>
                                        <div class="d-flex mt-3">
                                            <a href="https://www.youtube.com/watch?v={{ video.id }}" target="_blank" class="btn btn-sm btn-outline-danger me-2">
                                                Watch
                                            </a>
                                            <button onclick="getTranscript('{{ video.id }}')" class="btn btn-sm btn-outline-primary">
                                                Get Transcript
                                            </button>
                                        </div>
                                    </div>
                                </div>
                                {% endif %}
                                {% endfor %}
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <!-- Shorts -->
    {% if channel.shorts and channel.shorts|length > 0 %}
    <div class="row mt-4">
        <div class="col-12">
            <div class="card">
                <div class="card-header">
                    <h4>Recent Shorts</h4>
                </div>
                <div class="card-body">
                    <div class="video-grid">
                        {% for short in channel.shorts %}
                        <div class="card">
                            <img src="{{ short.thumbnail }}" class="card-img-top" alt="{{ short.title }}">
                            <div class="card-body">
                                <h5 class="card-title">
                                    {{ short.title }}
                                    <span class="short-badge">SHORT</span>
                                </h5>
                                <p class="card-text text-muted">{{ short.published.split('T')[0] }}</p>
                                <div class="video-stats">
                                    <span><i class="far fa-eye"></i> {{ short.views }}</span>
                                    <span><i class="far fa-thumbs-up"></i> {{ short.likes }}</span>
                                    <span><i class="far fa-comment"></i> {{ short.comments }}</span>
                                </div>
                                <div class="d-flex mt-3">
                                    <a href="https://www.youtube.com/watch?v={{ short.id }}" target="_blank" class="btn btn-sm btn-outline-danger me-2">
                                        Watch
                                    </a>
                                    <button onclick="getTranscript('{{ short.id }}')" class="btn btn-sm btn-outline-primary">
                                        Get Transcript
                                    </button>
                                </div>
                            </div>
                        </div>
                        {% endfor %}
                    </div>
                </div>
            </div>
        </div>
    </div>
    {% endif %}
</div>

<script>
    {% if channel.charts['7d'] %}
        var chart7d = {{ channel.charts['7d'] | safe }};
        Plotly.newPlot('chart-7d', chart7d.data, chart7d.layout);
    {% endif %}
    
    {% if channel.charts['30d'] %}
        var chart30d = {{ channel.charts['30d'] | safe }};
        Plotly.newPlot('chart-30d', chart30d.data, chart30d.layout);
    {% endif %}
    
    {% if channel.charts['6m'] %}
        var chart6m = {{ channel.charts['6m'] | safe }};
        Plotly.newPlot('chart-6m', chart6m.data, chart6m.layout);
    {% endif %}
</script>
'''

# Transcript page HTML template
TRANSCRIPT_HTML = '''
<div class="container mt-5">
    <div class="card">
        <div class="card-header">
            <h3>Video Transcript</h3>
        </div>
        <div class="card-body">
            <div class="transcript-header">
                <div class="row">
                    <div class="col-md-8">
                        <h4>{{ transcript.title }}</h4>
                        <p>Author: {{ transcript.author }}</p>
                    </div>
                    <div class="col-md-4 text-end">
                        <a href="https://www.youtube.com/watch?v={{ video_id }}" target="_blank" class="btn btn-danger">
                            <i class="fab fa-youtube"></i> Watch Video
                        </a>
                        <a href="/download-transcript/{{ video_id }}" class="btn btn-outline-primary ms-2">
                            <i class="fas fa-download"></i> Download
                        </a>
                    </div>
                </div>
            </div>
            
            <div class="transcript-text">
                {{ transcript.transcript }}
            </div>
        </div>
    </div>
    
    <div class="mt-4 text-center">
        <a href="javascript:history.back()" class="btn btn-outline-secondary">
            <i class="fas fa-arrow-left"></i> Back to Analysis
        </a>
    </div>
</div>
'''

# ---------------- ROUTES ----------------
@app.route('/')
def index():
    return render_template_string(BASE_HTML, content=INDEX_HTML)

@app.route('/analyze', methods=['POST'])
def analyze():
    channel_url = request.form.get('channel_url', '')
    
    try:
        # Get channel ID
        channel_id = get_channel_id(channel_url)
        
        # Get channel info
        channel_info = fetch_channel_info(channel_id)
        if not channel_info:
            return jsonify({"error": "Could not fetch channel information"}), 400
            
        # Get videos
        videos = fetch_videos(channel_id)
        
        # Get shorts specifically (the API doesn't have a direct way to fetch only shorts)
        shorts = fetch_shorts(channel_id)
        
        # Process channel data
        stats = channel_info["statistics"]
        total_views = int(stats.get("viewCount", 0))
        monthly_income, lifetime_income = estimate_income(total_views)
        description = channel_info["snippet"].get("description", "")
        contact_email = extract_email(description)
        social_links = extract_social_links(description)
        
        # Generate growth charts
        charts = generate_growth_chart(videos)
        
        # Sort videos by date (newest first)
        latest_videos = sorted(videos, key=lambda v: v["snippet"]["publishedAt"], reverse=True)[:10]
        
        # Format video data for template and identify shorts
        formatted_videos = []
        shorts_count = 0
        
        for v in latest_videos:
            # Check if it's likely a short
            is_short = False
            if "contentDetails" in v and "duration" in v["contentDetails"]:
                duration = v["contentDetails"]["duration"]
                if 'H' not in duration and ('30S' in duration or '20S' in duration or '10S' in duration or '15S' in duration or 'M1' in duration):
                    is_short = True
                    shorts_count += 1
            
            formatted_videos.append({
                'id': v['id'],
                'title': v['snippet']['title'],
                'published': v['snippet']['publishedAt'],
                'views': v['statistics'].get('viewCount', '0'),
                'likes': v['statistics'].get('likeCount', '0'),
                'comments': v['statistics'].get('commentCount', '0'),
                'thumbnail': v['snippet']['thumbnails']['medium']['url'] if 'thumbnails' in v['snippet'] and 'medium' in v['snippet']['thumbnails'] else '',
                'isShort': is_short
            })
        
        # Format shorts specifically
        formatted_shorts = []
        for s in shorts:
            formatted_shorts.append({
                'id': s['id'],
                'title': s['snippet']['title'],
                'published': s['snippet']['publishedAt'],
                'views': s['statistics'].get('viewCount', '0'),
                'likes': s['statistics'].get('likeCount', '0'),
                'comments': s['statistics'].get('commentCount', '0'),
                'thumbnail': s['snippet']['thumbnails']['medium']['url'] if 'thumbnails' in s['snippet'] and 'medium' in s['snippet']['thumbnails'] else ''
            })
        
        # Prepare data for template
        channel_data = {
            'id': channel_id,
            'title': channel_info['snippet']['title'],
            'description': description,
            'subscribers': stats.get('subscriberCount', 'Hidden'),
            'views': total_views,
            'videoCount': stats.get('videoCount', 'N/A'),
            'shortsCount': shorts_count,
            'created': channel_info['snippet']['publishedAt'],
            'country': channel_info['snippet'].get('country', 'N/A'),
            'thumbnail': channel_info['snippet']['thumbnails']['high']['url'] if 'thumbnails' in channel_info['snippet'] and 'high' in channel_info['snippet']['thumbnails'] else '',
            'banner': channel_info['brandingSettings']['image'].get('bannerExternalUrl', '') if 'image' in channel_info['brandingSettings'] else '',
            'monthlyIncome': f"${monthly_income[0]} - ${monthly_income[1]}",
            'lifetimeIncome': f"${lifetime_income[0]} - ${lifetime_income[1]}",
            'email': contact_email,
            'socialLinks': social_links,
            'videos': formatted_videos,
            'shorts': formatted_shorts,
            'charts': charts
        }
        
        return render_template_string(BASE_HTML, content=render_template_string(RESULT_HTML, channel=channel_data))
        
    except Exception as e:
        error_message = f"<div class='container mt-5'><div class='alert alert-danger'><h4>Error</h4><p>{str(e)}</p><a href='/' class='btn btn-primary'>Try Again</a></div></div>"
        return render_template_string(BASE_HTML, content=error_message)

@app.route('/download-csv/<channel_id>')
def download_csv(channel_id):
    try:
        channel_info = fetch_channel_info(channel_id)
        videos = fetch_videos(channel_id)
        
        csv_data = generate_csv(channel_info, videos)
        
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-disposition": f"attachment; filename=youtube_report_{channel_id}.csv"}
        )
        
    except Exception as e:
        error_message = f"<div class='container mt-5'><div class='alert alert-danger'><h4>Error</h4><p>{str(e)}</p><a href='/' class='btn btn-primary'>Go Back</a></div></div>"
        return render_template_string(BASE_HTML, content=error_message)

@app.route('/transcript/<video_id>')
def transcript(video_id):
    try:
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        transcript_data = get_transcript(video_url)
        
        return render_template_string(
            BASE_HTML, 
            content=render_template_string(
                TRANSCRIPT_HTML, 
                transcript=transcript_data, 
                video_id=video_id
            )
        )
        
    except Exception as e:
        error_message = f"<div class='container mt-5'><div class='alert alert-danger'><h4>Error Getting Transcript</h4><p>{str(e)}</p><a href='javascript:history.back()' class='btn btn-primary'>Go Back</a></div></div>"
        return render_template_string(BASE_HTML, content=error_message)

@app.route('/download-transcript/<video_id>')
def download_transcript(video_id):
    try:
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        transcript_data = get_transcript(video_url)
        
        transcript_text = f"Title: {transcript_data['title']}\n"
        transcript_text += f"Author: {transcript_data['author']}\n\n"
        transcript_text += transcript_data['transcript']
        
        return Response(
            transcript_text,
            mimetype="text/plain",
            headers={"Content-disposition": f"attachment; filename=transcript_{video_id}.txt"}
        )
        
    except Exception as e:
        error_message = f"<div class='container mt-5'><div class='alert alert-danger'><h4>Error</h4><p>{str(e)}</p><a href='javascript:history.back()' class='btn btn-primary'>Go Back</a></div></div>"
        return render_template_string(BASE_HTML, content=error_message)

if __name__ == '__main__':
    app.run(debug=True)
