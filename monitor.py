import asyncio
import html
import http.cookiejar
import json
import logging
import os
import re
import urllib.parse
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import aiohttp

# Japan Standard Time (JST = UTC+9)
JST = timezone(timedelta(hours=9))
CONFIG_FILE = "config.json"
DEFAULT_SOCS_COOKIE = "CAESEwgDEgk2NDg4NTY2OTgaAnJvIAEaBgiAtae0Bg"

def load_json(filepath, default):
    if not os.path.exists(filepath):
        return default
    with open(filepath, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return default

def write_string_atomic(filepath, data_str):
    tmp_path = f"{filepath}.tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        f.write(data_str)
    os.replace(tmp_path, filepath)

class YouTubePostTelegramMonitor:
    def __init__(self, config):
        self.config = config
        self.interval = config.get("CHECK_INTERVAL", 180)
        self.target_channels = config.get("TARGET_CHANNELS", [])
        
        self.state_file = config.get("SEED_FILE", "seen_posts.json")
        self.max_history = config.get("MAX_HISTORY", 1000)
        
        self.seed_ids = OrderedDict.fromkeys(load_json(self.state_file, []))
        self.state_io_lock = asyncio.Lock()
        
        self.is_first_run = len(self.seed_ids) == 0
        
        self.telegram_token = config.get("TELEGRAM_TOKEN")
        self.telegram_chat_id = config.get("TELEGRAM_CHAT_ID")
        
        self.cookie_file = config.get("COOKIE_FILE", "cookies.txt")
        self.session_cookies = {"SOCS": DEFAULT_SOCS_COOKIE}
        
        if os.path.exists(self.cookie_file):
            try:
                cj = http.cookiejar.MozillaCookieJar(self.cookie_file)
                cj.load(ignore_discard=True, ignore_expires=True)
                for cookie in cj:
                    if "youtube.com" in cookie.domain:
                        self.session_cookies[cookie.name] = cookie.value
                logging.info(f"Loaded {len(self.session_cookies)-1} YouTube cookies from {self.cookie_file}")
            except Exception as e:
                logging.error(f"Failed to load cookies from {self.cookie_file}: {e}")
        else:
            logging.warning(f"Cookie file '{self.cookie_file}' not found. Using default SOCS cookie.")
        
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"
        }
        
        self.session = None
        self.notification_queue = asyncio.Queue()
        
        logging.basicConfig(
            level=logging.INFO,
            format="[%(asctime)s] %(message)s",
            datefmt="%H:%M:%S",
            force=True
        )

    def _mark_seen(self, post_id):
        self.seed_ids[post_id] = None
        while len(self.seed_ids) > self.max_history:
            self.seed_ids.popitem(last=False)

    async def save_state(self):
        async with self.state_io_lock:
            keys_to_save = list(self.seed_ids.keys())
            json_str = json.dumps(keys_to_save, indent=2)
            await asyncio.to_thread(write_string_atomic, self.state_file, json_str)

    def _build_posts_url(self, target: str) -> str:
        clean = target.strip().rstrip("/")
        if clean.endswith("/posts") or clean.endswith("/community"):
            clean = clean.rsplit("/", 1)[0]
        if clean.startswith("http://") or clean.startswith("https://"):
            return f"{clean}/posts"
        if clean.startswith("@"):
            return f"https://www.youtube.com/{clean}/posts"
        if clean.startswith("UC"):
            return f"https://www.youtube.com/channel/{clean}/posts"
        return f"https://www.youtube.com/@{clean}/posts"

    def _extract_initial_data(self, html_text: str) -> dict | None:
        patterns = [
            r"ytInitialData\s*=\s*({.+?});\s*</script>",
            r"var\s+ytInitialData\s*=\s*({.+?});"
        ]
        for pattern in patterns:
            match = re.search(pattern, html_text)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    continue
        return None

    def _parse_relative_time(self, time_str: str) -> datetime:
        now = datetime.now(JST)
        if not time_str:
            return now
        # Extract magnitude and unit (e.g., "2 hours ago")
        match = re.search(r'(\d+)\s+(second|minute|hour|day|week|month|year)s?', time_str)
        if match:
            val = int(match.group(1))
            unit = match.group(2)
            if unit == 'second': delta = timedelta(seconds=val)
            elif unit == 'minute': delta = timedelta(minutes=val)
            elif unit == 'hour': delta = timedelta(hours=val)
            elif unit == 'day': delta = timedelta(days=val)
            elif unit == 'week': delta = timedelta(weeks=val)
            elif unit == 'month': delta = timedelta(days=val * 30.4)
            elif unit == 'year': delta = timedelta(days=val * 365)
            else: delta = timedelta(0)
            return now - delta
        return now

    def _parse_post_item(self, item_renderer: dict) -> dict | None:
        post_thread = item_renderer.get("backstagePostThreadRenderer", {})
        post_wrapper = post_thread.get("post", {})
        
        renderer = post_wrapper.get("backstagePostRenderer") or post_wrapper.get("sharedPostRenderer")
        if not renderer:
            return None
            
        post_id = renderer.get("postId")
        if not post_id:
            return None
            
        is_membership = "sponsorsOnlyBadge" in renderer
            
        author_name = "YouTube Channel"
        author_text_obj = renderer.get("authorText")
        if author_text_obj and "runs" in author_text_obj:
            author_name = "".join([r.get("text", "") for r in author_text_obj["runs"]])
            
        content_obj = renderer.get("contentText") or renderer.get("content")
        post_text = ""
        if content_obj and "runs" in content_obj:
            for r in content_obj["runs"]:
                # Parse endpoints to prevent URL truncation
                if "navigationEndpoint" in r:
                    nav = r["navigationEndpoint"]
                    # External Links (e.g. Redirects)
                    if "urlEndpoint" in nav:
                        url = nav["urlEndpoint"].get("url", "")
                        match = re.search(r"(?:[?&])q=([^&]+)", url)
                        if match:
                            url = urllib.parse.unquote(match.group(1))
                        post_text += url
                    # Internal YouTube Links (e.g. Channels, Videos, Hashtags)
                    elif "commandMetadata" in nav and "webCommandMetadata" in nav["commandMetadata"]:
                        url = nav["commandMetadata"]["webCommandMetadata"].get("url", "")
                        if url.startswith("/"):
                            url = "https://www.youtube.com" + url
                        post_text += url
                    else:
                        post_text += r.get("text", "")
                else:
                    post_text += r.get("text", "")
            
        published_time_str = "Recently"
        pub_obj = renderer.get("publishedTimeText")
        if pub_obj and "runs" in pub_obj and pub_obj["runs"]:
            published_time_str = pub_obj["runs"][0].get("text", "Recently")
            
        media_url = None
        attachment = renderer.get("backstageAttachment", {})
        if "backstageImageRenderer" in attachment:
            thumbnails = attachment["backstageImageRenderer"].get("image", {}).get("thumbnails", [])
            if thumbnails:
                media_url = thumbnails[-1].get("url")
        elif "postMultiImageRenderer" in attachment:
            images = attachment["postMultiImageRenderer"].get("images", [])
            if images:
                first_img = images[0].get("backstageImageRenderer", {}).get("image", {}).get("thumbnails", [])
                if first_img:
                    media_url = first_img[-1].get("url")
        elif "videoRenderer" in attachment:
            video_id = attachment["videoRenderer"].get("videoId")
            if video_id:
                media_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
            else:
                thumb_list = attachment["videoRenderer"].get("thumbnail", {}).get("thumbnails", [])
                if thumb_list:
                    media_url = thumb_list[-1].get("url")
                    
        return {
            "post_id": post_id,
            "post_url": f"https://www.youtube.com/post/{post_id}",
            "author": author_name,
            "text": post_text,
            "published_time_str": published_time_str,
            "media_url": media_url,
            "is_membership": is_membership
        }

    async def fetch_channel_posts(self, target: str) -> list[dict]:
        url = self._build_posts_url(target)
        try:
            async with self.session.get(url, headers=self.headers, timeout=30) as resp:
                if resp.status != 200:
                    logging.warning(f"HTTP {resp.status} while fetching {url}")
                    return []
                html_text = await resp.text()
                
            data = self._extract_initial_data(html_text)
            if not data:
                logging.warning(f"Failed to extract ytInitialData from {url}")
                return []
                
            tabs = data.get("contents", {}).get("twoColumnBrowseResultsRenderer", {}).get("tabs", [])
            items = []
            
            for tab in tabs:
                tab_renderer = tab.get("tabRenderer", {})
                tab_url = tab_renderer.get("endpoint", {}).get("commandMetadata", {}).get("webCommandMetadata", {}).get("url", "")
                tab_title = tab_renderer.get("title", "")
                
                if tab_title in ["Posts", "Community", "貼文", "社区"] or tab_url.endswith("/posts") or tab_url.endswith("/community"):
                    section_contents = tab_renderer.get("content", {}).get("sectionListRenderer", {}).get("contents", [])
                    if section_contents:
                        items = section_contents[0].get("itemSectionRenderer", {}).get("contents", [])
                    break
                    
            parsed_posts = []
            # Slice the items array to parse only the 3 most recent posts
            for item in items[:3]:
                post = self._parse_post_item(item)
                if post:
                    parsed_posts.append(post)
            return parsed_posts
            
        except Exception as e:
            logging.error(f"Error fetching channel posts for {target}: {e}")
            return []

    async def telegram_worker(self):
        while True:
            payload = await self.notification_queue.get()
            api_endpoint = "sendMessage" if 'text' in payload else "sendPhoto"
            api_url = f"https://api.telegram.org/bot{self.telegram_token}/{api_endpoint}"
            retries = 0
            max_retries = 3
            
            if not payload.get('photo'):
                payload.pop('photo', None)
                api_endpoint = "sendMessage"
                api_url = f"https://api.telegram.org/bot{self.telegram_token}/{api_endpoint}"
                payload['text'] = payload.pop('caption', '')
                
            try:
                while retries < max_retries:
                    try:
                        async with self.session.post(api_url, json=payload, timeout=60) as resp:
                            if resp.status == 429:
                                retry_after = int(resp.headers.get("Retry-After", 5))
                                logging.warning(f"Telegram 429 Rate Limit. Sleeping {retry_after}s.")
                                await asyncio.sleep(retry_after)
                                retries += 1
                                continue
                            if resp.status >= 500:
                                await asyncio.sleep(5)
                                retries += 1
                                continue
                            resp.raise_for_status()
                            break
                    except asyncio.TimeoutError:
                        await asyncio.sleep(5)
                        retries += 1
                    except aiohttp.ClientError as e:
                        logging.error(f"Telegram ClientError: {e}")
                        break
                if retries >= max_retries:
                    logging.error("Telegram payload failed. Re-queueing with delay.")
                    await asyncio.sleep(30)
                    await self.notification_queue.put(payload)
            except Exception as e:
                logging.error(f"Telegram Worker Error: {e}")
            finally:
                self.notification_queue.task_done()
                await asyncio.sleep(1.5)

    async def process_channel(self, target: str):
        posts = await self.fetch_channel_posts(target)
        if not posts:
            return
            
        state_modified = False
        for post in reversed(posts):
            post_id = post["post_id"]
            if post_id in self.seed_ids:
                continue
                
            if self.is_first_run:
                self._mark_seen(post_id)
                state_modified = True
                continue
            
            exact_time = self._parse_relative_time(post['published_time_str'])
            time_str = f" | Time: {exact_time.strftime('%Y-%m-%d %H:%M')}"
            is_membership = post.get("is_membership", False)
            
            if is_membership:
                first_line = post['text'].strip().split('\n')[0] if post['text'].strip() else "Membership Post"
                content_text = f"{html.escape(first_line)} — MEMBERSHIP ONLY"
                media_url = None
            else:
                content_text = html.escape(post['text'])
                media_url = post.get("media_url")
                
            caption_parts = [
                f"<b>[YOUTUBE POST]{time_str}</b>",
                content_text,
                html.escape(post['author']),
                post['post_url']
            ]
            
            notif_payload = {
                'chat_id': self.telegram_chat_id,
                'caption': '\n'.join(caption_parts),
                'parse_mode': 'HTML'
            }
            if media_url:
                notif_payload['photo'] = media_url
            
            logging.info(f"Queuing Telegram notification: Post {post_id} from {post['author']}")
            await self.notification_queue.put(notif_payload)
            self._mark_seen(post_id)
            state_modified = True
            
        if state_modified:
            await self.save_state()

    async def monitoring_worker(self):
        while True:
            for channel in self.target_channels:
                await self.process_channel(channel)
                await asyncio.sleep(2)
                
            if self.is_first_run:
                self.is_first_run = False
                logging.info(f"Initial post seeding complete. Seeded {len(self.seed_ids)} posts.")
                
            await asyncio.sleep(self.interval)

    async def run(self):
        logging.info(f"YouTube Post Telegram Monitor started. Check interval: {self.interval}s")
        async with aiohttp.ClientSession(cookies=self.session_cookies) as session:
            self.session = session
            tasks = [
                asyncio.create_task(self.telegram_worker()),
                asyncio.create_task(self.monitoring_worker())
            ]
            await asyncio.gather(*tasks)

async def main():
    config = load_json(CONFIG_FILE, {})
    if not config:
        logging.error("Configuration file missing. Generating template config.json...")
        template = {
            "CHECK_INTERVAL": 180,
            "TARGET_CHANNELS": ["@NinomaeInanis", "https://www.youtube.com/@3blue1brown"],
            "COOKIE_FILE": "cookies.txt",
            "TELEGRAM_TOKEN": "YOUR_TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID": "-100YOUR_CHAT_ID",
            "SEED_FILE": "seen_posts.json",
            "MAX_HISTORY": 1000
        }
        write_string_atomic(CONFIG_FILE, json.dumps(template, indent=2))
        return
        
    monitor = YouTubePostTelegramMonitor(config)
    await monitor.run()

if __name__ == '__main__':
    asyncio.run(main())