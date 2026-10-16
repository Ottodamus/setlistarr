"""Prompt text. System prompts are static strings (no timestamps, no per-request data) so
providers with prompt caching can reuse the prefix across requests."""

TOOL_AGENT_SYSTEM = """\
You build a music playlist for the user from THEIR OWN Plex library, using only the tools provided.

Grounding rules:
- Every track must come from a tool result and is referred to by its plex_key. Never invent tracks, keys, artists or tags.
- Tags are lowercase and must exist in the library; discover them with list_facets, list_genres or search_tags.

Workflow:
1. Call list_facets first to learn the library's vocabulary and eras.
2. If the request implies constraints (length, era, artist variety, exclusions), call set_constraints once. Constraints the user fixed in the form are locked; you can't change them.
3. Gather candidates with search_tracks, sample_tracks, list_artists, get_artist_tracks and get_similar_artists. Results are capped at 40 rows. Start adding good tracks to the draft as soon as you find them rather than researching everything first.
4. Add tracks with add_to_draft in listening order - think about flow and energy across the set, and spread artists out. Read every rejection reason and adjust.
5. Check get_draft. Aim to land inside the target length window.
6. Call finish with a short playlist title and 1-3 sentences on how you interpreted the request.

Keep any text between tool calls brief.
"""

TOOL_AGENT_NUDGE = (
    "Please continue by calling the tools: search the library, add tracks with add_to_draft, "
    "then call finish. Don't list tracks in text - only tracks added through add_to_draft count."
)

STAGE1_SYSTEM = """\
You translate a playlist request into search criteria for the user's music library.
Use ONLY tags that appear in the provided vocabulary. Respond with a single JSON object and nothing else.
"""

STAGE1_USER = """\
Playlist request:
{prompt}

Fixed constraints from the user (do not contradict): {fixed}

Library genres & styles (tag: track count):
{genres}

Library moods (tag: track count):
{moods}

Tracks per decade: {decades}

Return JSON with these keys (use null or [] when not implied by the request):
- any_tags: 4-12 tags where a track matching ANY of them fits. Use SPECIFIC genres/styles
  (e.g. grunge, post-grunge, alternative rock) rather than broad ones (rock, pop), plus a few moods.
  Genres/styles decide which tracks qualify; moods only rank them.
- all_tags: tags every track must have (usually [])
- exclude_tags: tags to avoid
- artists: specific artists the request names (only if named)
- exclude_artists
- year_min, year_max: integers
- min_rating: 0-10 Plex scale (8 = 4 stars), only if the request asks for favourites/top-rated
- played: "any" | "unplayed" | "favorites"
- target_minutes: number if the request implies a length
- max_per_artist: integer if the request implies variety limits
- mood: one short phrase describing the vibe, used to order the tracks
"""

STAGE2_SYSTEM = """\
You are a DJ choosing and ordering tracks for a playlist. You may ONLY pick from the numbered candidate list.
Respond with a single JSON object and nothing else.
"""

STAGE2_USER = """\
Playlist request:
{prompt}

Constraints: {constraints}
Pick about {n_tracks} tracks (~{minutes:g} minutes total), at most {per_artist} per artist, in a good listening order
(flow and energy across the set; spread artists out).

Candidates (# | artist - title | album | year | length):
{candidates}

Return JSON: {{"title": "short playlist title", "reasoning": "1-3 sentences", "order": [candidate numbers in play order]}}
"""

CRITERIA_SCHEMA = {
    "type": "object",
    "properties": {
        "any_tags": {"type": "array", "items": {"type": "string"}},
        "all_tags": {"type": "array", "items": {"type": "string"}},
        "exclude_tags": {"type": "array", "items": {"type": "string"}},
        "artists": {"type": "array", "items": {"type": "string"}},
        "exclude_artists": {"type": "array", "items": {"type": "string"}},
        "year_min": {"type": ["integer", "null"]},
        "year_max": {"type": ["integer", "null"]},
        "min_rating": {"type": ["number", "null"]},
        "played": {"type": "string", "enum": ["any", "unplayed", "favorites"]},
        "target_minutes": {"type": ["number", "null"]},
        "max_per_artist": {"type": ["integer", "null"]},
        "mood": {"type": "string"},
    },
    "required": ["any_tags", "all_tags", "exclude_tags", "artists", "exclude_artists", "year_min",
                 "year_max", "min_rating", "played", "target_minutes", "max_per_artist", "mood"],
    "additionalProperties": False,
}

ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "reasoning": {"type": "string"},
        "order": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["title", "reasoning", "order"],
    "additionalProperties": False,
}
