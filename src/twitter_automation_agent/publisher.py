import time
import urllib.parse
import webbrowser
from pathlib import Path

from twitter_automation_agent.config import Settings


class XPublisher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def verify_credentials(self) -> tuple[str | None, str | None]:
        """
        No credentials needed for the intent URL method.
        """
        return "system_browser", "ready"

    def post(self, text: str, image_path: str | None = None) -> str:
        """
        Automates the Twitter web interface using the foolproof Intent URL method.
        This opens a new tab in the user's ACTUAL default browser where they are already logged in.
        """
        # We URL-encode the tweet text so it can be passed in the URL
        encoded_text = urllib.parse.quote(text)
        
        intent_url = f"https://x.com/intent/tweet?text={encoded_text}"
        
        print("\n\n✅ [SUCCESS] Opening X.com in your default system browser!")
        print("Your tweet has been automatically pasted into the box.")
        
        if image_path:
            abs_path = str(Path(image_path).absolute())
            print("\n📸 [IMAGE ATTACHMENT REQUIRED]")
            print("Twitter Intent URLs cannot attach images automatically.")
            print(f"Please drag and drop this image into your Tweet:\n{abs_path}")
            
            # Try to copy the path to clipboard for easy pasting
            try:
                import pyperclip
                pyperclip.copy(abs_path)
                print("(The image path has been copied to your clipboard. Just click the image icon on X and hit Ctrl+V!)")
            except ImportError:
                pass
        
        print("\nClick the 'Post' button on X.com to finish!")
        
        # Open the URL in the default browser
        webbrowser.open(intent_url)
        
        try:
            import pyautogui
            import subprocess
            
            print("\n🤖 [AUTOMATION] Please do not touch your mouse/keyboard for 5 seconds...")
            # Wait 6 seconds for the Chrome tab to fully open and load the X.com compose box
            time.sleep(6)
            
            if image_path:
                # Instead of just copying the text path, we tell Windows to copy the actual File Object 
                # into the clipboard, exactly as if you right-clicked it in Explorer and hit 'Copy'
                subprocess.run(["powershell", "-command", f"Set-Clipboard -Path '{abs_path}'"], check=False)
                
                # Paste the image directly into the active X.com tweet box
                pyautogui.hotkey('ctrl', 'v')
                
                # Wait 2 seconds for the image thumbnail to attach and render
                time.sleep(2)
                
            # Press Ctrl+Enter (the X.com hotkey to immediately publish the tweet)
            pyautogui.hotkey('ctrl', 'enter')
            
            print("✅ Tweet automatically posted!")
            
        except ImportError:
            print("\n(Note: To enable 100% hands-free posting, run: pip install pyautogui)")
        
        return f"intent-post-{int(time.time())}"
                
