"""Versioned source suggestions. User sources are merged once, never replaced."""
PRESETS = {
    'finance': [
        'https://www.finam.ru/analysis/conews/rsspoint/',
        'https://www.finam.ru/international/advanced/rsspoint/',
        'https://www.cbr.ru/rss/eventrss',
    ],
    'football': [
        'https://www.euro-football.ru/feed',
        'https://www.euro-football.ru/category/rss/29',
        'https://www.theguardian.com/football/rss',
    ],
    'ai': [
        'https://www.theguardian.com/technology/artificialintelligenceai/rss',
        'https://openai.com/news/rss.xml',
        'https://blogs.microsoft.com/ai/feed/',
        'https://huggingface.co/blog/feed.xml',
        'https://blog.google/technology/ai/rss/',
        'https://habr.com/ru/rss/hubs/artificial_intelligence/articles/?fl=ru',
    ],
    'cinema': [
        'https://thefilmstage.com/feed/',
        'https://www.theguardian.com/film/rss',
        'https://www.denofgeek.com/movies/feed/',
    ],
}


def preset_for(name, config):
    """Recognize existing channels without inventing or renaming channels."""
    name = name.casefold()
    sources = set(config.get('sources', []))
    if config.get('mode') == 'fashion':
        return None
    if 'футбол' in name or 'football' in name:
        return 'football'
    if 'капитал по делу' in name:
        return 'finance'
    if 'нейросет' in name or sources.intersection(PRESETS['ai'][:3]):
        return 'ai'
    if config.get('mode') == 'cinema':
        return 'cinema'
    return None
