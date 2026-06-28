from __future__ import annotations

from pathlib import Path

import tweepy

from twitter_automation_agent.config import Settings


class XPublisher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def verify_credentials(self) -> tuple[str | None, str | None]:
        if not self.settings.can_post_to_x:
            raise RuntimeError("X API credentials are not fully configured.")
        try:
            user = self._api_v1().verify_credentials()
        except tweepy.Unauthorized as exc:
            raise RuntimeError(
                "X rejected the OAuth 1.0a credentials. Regenerate the Access Token and "
                "Access Token Secret after setting app permissions to Read and write."
            ) from exc
        return getattr(user, "screen_name", None), getattr(user, "id_str", None)

    def post(self, text: str, image_path: str | None = None) -> str:
        if not self.settings.can_post_to_x:
            raise RuntimeError("X API credentials are not fully configured.")

        api_v1 = self._api_v1()
        client = self._client_v2()

        media_ids: list[str] = []
        if image_path:
            try:
                media = api_v1.media_upload(filename=str(Path(image_path)))
            except tweepy.Unauthorized as exc:
                raise RuntimeError(
                    "X media upload failed because OAuth 1.0a credentials were rejected. "
                    "Regenerate Access Token and Access Token Secret with Read and write permissions."
                ) from exc
            media_ids.append(media.media_id_string)

        try:
            response = client.create_tweet(text=text, media_ids=media_ids or None)
        except tweepy.Unauthorized as exc:
            raise RuntimeError(
                "X tweet creation failed because OAuth 1.0a credentials were rejected. "
                "Regenerate Access Token and Access Token Secret with Read and write permissions."
            ) from exc
        tweet_id = response.data.get("id") if response.data else None
        if not tweet_id:
            raise RuntimeError("X API did not return a tweet id.")
        return str(tweet_id)

    def _api_v1(self) -> tweepy.API:
        auth = tweepy.OAuth1UserHandler(
            self.settings.x_api_key,
            self.settings.x_api_secret,
            self.settings.x_access_token,
            self.settings.x_access_token_secret,
        )
        api_v1 = tweepy.API(auth)
        api_v1.session.trust_env = False
        return api_v1

    def _client_v2(self) -> tweepy.Client:
        client = tweepy.Client(
            consumer_key=self.settings.x_api_key,
            consumer_secret=self.settings.x_api_secret,
            access_token=self.settings.x_access_token,
            access_token_secret=self.settings.x_access_token_secret,
        )
        client.session.trust_env = False
        return client
