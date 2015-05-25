import asyncio
import aiohttp

import json
from urllib.parse import urlencode
from xml.dom import minidom

from functools import lru_cache
from warnings import warn

def xml_get_text(tag):
    parts = []
    for node in tag.childNodes:
        if node.nodeType == node.TEXT_NODE:
            parts.append(node.data)
    return "".join(parts)


class User:

    def __init__(self, username, tracks, **kwargs):
        self.username = username
        self.tracks = tracks
        self.listencount = kwargs.get("listencount", None)
        self.birthday = kwargs.get("birthday", None)

    def now_listening(self):
        """Check if a user is listening to a track"""
        if not self.tracks:
            return False

        return self.tracks[0].playing


class Tag:

    def __init__(self, tag, **kwargs):
        self.tag = tag
        self.url = kwargs.get("url", None)
        self.reach = kwargs.get("reach", None)
        self.taggings = kwargs.get("taggings", None)
        self.toptracks = kwargs.get("toptracks", None)
        self.topartists = kwargs.get("topartists", None)


class Track:
    def __init__(self, artist, title, **kwargs):
        self.artist = artist
        self.title = title
        self.album = kwargs.get("album", None)
        self.tags = kwargs.get("tags", None)
        self.duration = kwargs.get("duration", None)
        self.loved = kwargs.get("loved", None)
        self.mbid = kwargs.get("mbid", None)
        self.playing = kwargs.get("playing", None)
        self.description = kwargs.get("description", None)

    def __str__(self):
        return "{title} by {artist}".format(title=self.title,
                                            artist=self.artist)

    def format(self, fmt, **props):
        d = {name: getattr(self, name) for name in self.__slots__
             if getattr(self, name, None) is not None}
        props.update(d)
        return fmt.format(**props)

    @classmethod
    def from_json(cls, json):
        kw = {}

        assert "name" in json
        assert "artist" in json

        if "#text" in json["artist"]:
            artist = json["artist"]["#text"]
        else:
            artist = str(json["artist"])

        title = json["name"]

        if "album" in json:
            if "#text" in json["album"]:
                kw["album"] = json["album"]["#text"]
            else:
                kw["album"] = str(json["album"])

        kw["playing"] = ("@attr" in json and "nowplaying" in json["@attr"])

        if "mbid" in json and json["mbid"] != "":
            kw["mbid"] = json["mbid"]

        if "loved" in json:  # indeterminate if it isn't present.
            kw["loved"] = (json["loved"] == "1")

        return cls(artist, title, **kw)

    @classmethod
    def from_xml(cls, xml):
        kw = {}

        artist = xml_get_text(xml.getElementsByTagName("artist")[0])
        title = xml_get_text(xml.getElementsByTagName("name")[0])

        album = xml_get_text(xml.getElementsByTagName("album")[0])
        if album != "":
            kw["album"] = album

        mbid = xml_get_text(xml.getElementsByTagName("mbid")[0])
        if mbid != "":
            kw["mbid"] = mbid

        kw["playing"] = xml.hasAttribute("nowplaying")

        loved_tag = xml.getElementsByTagName("loved")
        if len(loved_tag) > 0:  # indeterminate if it isn't present.
            kw["loved"] = (xml_get_text(loved_tag[0]) == "1")

        return cls(artist, title, **kw)


class LastFMError(Exception):
    
    """Error raised when last.fm API throws an error"""

    def __init__(self, errorcode, error):
        super().__init__(errorcode, error)
        self.errorcode = errorcode
        self.error = error


class LastFM:

    """The last.fm class, which contains all the functions to do API calls"""

    url = "http://ws.audioscrobbler.com/2.0/"
    """The last.fm API endpoint"""

    def __init__(self, api_key, fmt="json"):
        """Initialise the last.fm class.

        :param api_key: The last.fm API key to use.

        :param fmt: Format to use the Last.FM API in
        """
        fmt = fmt.lower()
        assert fmt in ("json", "xml"), "API format must be json or XML"

        self.api_key = api_key
        self.fmt = fmt

    @lru_cache(maxsize=16)
    def parse_data(self, response):
        """Parse last.fm data.

        :param response: The raw response to parse.

        :returns: A tuple containing the data format and the data.
        """
        if self.fmt == "json":
            return json.loads(response)
        else:
            return minidom.parseString(response)

    @lru_cache(maxsize=32)
    def build_qs(self, **keys):
        """Build a query string for the last.fm API"""
        keys["api_key"] = self.api_key

        return "{}?{}".format(self.url, urlencode(keys))

    @asyncio.coroutine
    def call_api(self, method, **keys):
        """Call the last.fm API directly using the given parameters.

        :param method: The API method to call (i.e. "track.getInfo").

        :returns: Response as parsed by :py:func:`parse_data`.
        """
        keys["method"] = method
        if self.fmt == "json":
            keys["format"] = self.fmt

        response = yield from aiohttp.request("GET", self.build_qs(**keys))

        if response.status != 200:
            try:
                data = yield from response.text()
            except Exception as e:
                data = "<Server error: {}>".format(str(e))

            raise LastFMError(response.status, data)

        data = yield from response.text()
        return self.parse_data(data)

    def get_tracks(self, user, limit=None):
        """Get the track(s) being listened to by a user.

        :param user: User to get listening data for

        :param limit: Limit the number of tracks, None for as many as the
            server gives us.
        """
        assert user, "User cannot be None or empty"

        keys = {
            "user": user,
            "extended": "1",
        }

        if limit is not None:
            keys["limit"] = limit

        data = yield from self.call_api("user.getRecentTracks", **keys)

        if self.fmt == "json":
            assert "recenttracks" in data and "track" in data["recenttracks"],\
                "Invalid response recieved"
            data = data["recenttracks"]["track"]
            if not isinstance(data, list):
                data = [data]

            return [Track.from_json(t) for t in data]
        elif self.fmt == "xml":
            tracks = data.getElementsByTagName("track")

            return [Track.from_xml(t) for t in tracks]

    def get_track_info(self, track, user=None):
        """Get the information on a track, returning user data optionally.

        :param track: Get info about this track, either an mbid, an
            (artist, title) tuple, or a
            :py:class:`~python-lastfm.lastfm.track` object

        :param user: Get user info about a track (play count, etc).
        """
        keys = {}

        if user:
            keys["username"] = user

        if hasattr(track, "mbid"):
            if track.mbid is not None:
                keys["mbid"] = track.mbid
            else:
                keys["artist"] = track.artist
                keys["track"] = track.track
        elif isinstance(track, str):
            keys["mbid"] = track
        else:
            keys["artist"] = track[0]
            keys["track"] = track[1]

        data = yield from self.call_api("track.getInfo", **keys)
        return data
