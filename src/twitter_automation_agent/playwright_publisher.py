import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from twitter_automation_agent.config import Settings


class PlaywrightPublisher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.user_data_dir = Path.home() / ".twitter_automation" / "playwright_data"
        self.user_data_dir.mkdir(parents=True, exist_ok=True)

    def verify_credentials(self) -> tuple[str | None, str | None]:
        """
        Ensures the user is logged into X.com. If not, opens a visible browser for them to log in.
        """
        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = browser.new_page()
            page.goto("https://x.com/")
            
            try:
                # Wait for either the post box (logged in) or the login button (logged out)
                page.wait_for_selector('a[data-testid="loginButton"], a[data-testid="SideNav_NewTweet_Button"]', timeout=10000)
            except Exception:
                pass
                
            is_logged_in = page.locator('a[data-testid="SideNav_NewTweet_Button"]').count() > 0
            
            if not is_logged_in:
                print("\n[REQUIRED] You are not logged into X.com in the Playwright automation browser.")
                print("Please log in now. The browser will stay open for 5 minutes.")
                try:
                    # Wait up to 5 minutes for the user to log in
                    page.wait_for_selector('a[data-testid="SideNav_NewTweet_Button"]', timeout=300000)
                    print("Login detected! Saved session.")
                except Exception:
                    browser.close()
                    raise RuntimeError("Login timed out. Please run the bot again to log in.")
                    
            browser.close()
            return "playwright", "ready"

    def post(self, text: str, image_paths: list[str] | None = None, thread_texts: list[str] | None = None, telegram_sender: Any = None) -> str:
        """
        Automates X.com via Playwright in the background and retrieves the posted Tweet ID via network interception.
        """
        print("\n[PLAYWRIGHT] Launching background browser...")
        
        with sync_playwright() as p:
            # We run headless=False so X.com doesn't aggressively block it.
            # Using headless=True often gets blocked by Cloudflare on X.com unless heavily spoofed.
            browser = p.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 720},
            )
            
            page = browser.new_page()
            
            def post_single_tweet(tweet_text: str, reply_to_id: str | None = None, images: list[str] | None = None) -> str | None:
                encoded_text = urllib.parse.quote(tweet_text)
                url = f"https://x.com/intent/tweet?text={encoded_text}"
                if reply_to_id:
                    url += f"&in_reply_to={reply_to_id}"
                    
                page.goto(url)
                
                # Wait for the compose box to appear
                try:
                    page.wait_for_selector('div[data-testid="tweetTextarea_0"]', timeout=15000)
                except Exception:
                    print("[PLAYWRIGHT] Failed to load compose box. Are you rate-limited?")
                    return None
                    
                if images:
                    abs_paths = [str(Path(p).absolute()) for p in images]
                    try:
                        file_input = page.locator('input[data-testid="fileInput"]')
                        file_input.set_input_files(abs_paths)
                        # Wait for images to attach
                        page.wait_for_selector('div[data-testid="attachments"]', timeout=15000)
                        time.sleep(3) # Extra buffer for upload completion
                    except Exception as e:
                        print(f"[PLAYWRIGHT] Failed to attach images: {e}")
                
                # Set up the network interceptor to catch the CreateTweet response
                with page.expect_response(lambda response: "CreateTweet" in response.url and response.status == 200, timeout=15000) as response_info:
                    try:
                        page.click('button[data-testid="tweetButton"]')
                    except Exception:
                        page.keyboard.press('Control+Enter')
                        
                response = response_info.value
                try:
                    data = response.json()
                    tweet_result = data.get("data", {}).get("create_tweet", {}).get("tweet_results", {}).get("result", {})
                    if "rest_id" in tweet_result:
                        return tweet_result["rest_id"]
                    print("[PLAYWRIGHT] Warning: Could not find rest_id in response. Raw response saved to logs.")
                except Exception as e:
                    print(f"[PLAYWRIGHT] Failed to parse CreateTweet response: {e}")
                    
                return None

            print("[PLAYWRIGHT] Posting root tweet...")
            root_tweet_id = post_single_tweet(text, images=image_paths)
            
            if not root_tweet_id:
                print("[PLAYWRIGHT] Failed to post root tweet or retrieve its ID.")
                browser.close()
                return f"playwright-fail-{int(time.time())}"
                
            print(f"[PLAYWRIGHT] Success! Root Tweet ID: {root_tweet_id}")
            
            if thread_texts and len(thread_texts) > 1:
                print(f"[PLAYWRIGHT] Detected {len(thread_texts)} total tweets in thread. Automating replies...")
                for i, next_tweet in enumerate(thread_texts[1:]):
                    if i > 0:
                        print(f"[PLAYWRIGHT] Waiting 3 minutes before posting the next reply...")
                        time.sleep(180)
                        
                    print(f"[PLAYWRIGHT] Posting reply {i+2}/{len(thread_texts)}...")
                    reply_id = post_single_tweet(next_tweet, reply_to_id=root_tweet_id)
                    if not reply_id:
                        print("[PLAYWRIGHT] Failed to post reply.")
                        break
                    print(f"[PLAYWRIGHT] Reply {i+2} posted successfully! ID: {reply_id}")
            
            browser.close()
            return root_tweet_id
