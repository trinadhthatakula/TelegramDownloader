"""Stable classification and forum routing for Telegram media."""
import mimetypes

from telethon import functions

CATEGORIES = ("images", "videos", "animations", "audio", "voice", "video_messages", "stickers", "documents")


def topic_id(message, forum=False):
    reply = getattr(message, "reply_to", None)
    if reply and getattr(reply, "forum_topic", False):
        return getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None) or 1
    return 1 if forum else None


def category(message):
    if getattr(message, "photo", None):
        return "images"
    document = getattr(message, "document", None)
    attrs = getattr(document, "attributes", ()) or ()
    names = {type(a).__name__: a for a in attrs}
    if "DocumentAttributeSticker" in names or "DocumentAttributeCustomEmoji" in names:
        return "stickers"
    if "DocumentAttributeAnimated" in names:
        return "animations"
    audio = names.get("DocumentAttributeAudio")
    if audio:
        return "voice" if getattr(audio, "voice", False) else "audio"
    video = names.get("DocumentAttributeVideo")
    if video and getattr(video, "round_message", False):
        return "video_messages"
    file = getattr(message, "file", None)
    mime = (getattr(document, "mime_type", None) or getattr(file, "mime_type", None)
            or mimetypes.guess_type(getattr(file, "name", "") or "x" + (getattr(file, "ext", "") or ""))[0]
            or "")
    if mime == "image/gif":
        return "animations"
    if mime.startswith("image/"):
        return "images"
    if video or mime.startswith("video/"):
        return "videos"
    if mime.startswith("audio/"):
        return "audio"
    return "documents"


def is_forum(entity):
    return bool(getattr(getattr(entity, "entity", entity), "forum", False))


def topics_request(entity, ids):
    # Telegram moved forum APIs from channels to messages in newer layers.
    cls = getattr(functions.messages, "GetForumTopicsByIDRequest", None)
    if cls:
        return cls(peer=entity, topics=ids)
    return functions.channels.GetForumTopicsByIDRequest(channel=entity, topics=ids)
