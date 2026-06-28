from __future__ import annotations

from pathlib import Path

import tweepy

from twitter_automation_agent.config import Settings


class XPublisher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def post(self, text: str, image_path: str | None = None) -> str:
        if not self.settings.can_post_to_x:
            raise RuntimeError("X API credentials are not fully configured.")

        auth = tweepy.OAuth1UserHandler(
            self.settings.x_api_key,
            self.settings.x_api_secret,
            self.settings.x_access_token,
            self.settings.x_access_token_secret,
        )
        api_v1 = tweepy.API(auth)

        media_ids: list[str] = []
        if image_path:
            media = api_v1.media_upload(filename=str(Path(image_path)))
            media_ids.append(media.media_id_string)

        client = tweepy.Client(
            consumer_key=self.settings.x_api_key,
            consumer_secret=self.settings.x_api_secret,
            access_token=self.settings.x_access_token,
            access_token_secret=self.settings.x_access_token_secret,
        )
        response = client.create_tweet(text=text, media_ids=media_ids or None)
        tweet_id = response.data.get("id") if response.data else None
        if not tweet_id:
            raise RuntimeError("X API did not return a tweet id.")
        return str(tweet_id)
