"""Progress pointers committed alongside validated observations/review records."""
import hashlib
import json
from models import AppSetting
from config import settings


def key(source,kind):
    seeds=json.loads(settings.scraper_seeds or '{}').get(source,{}).get(kind,[])
    digest=hashlib.sha256(json.dumps(seeds,sort_keys=True).encode()).hexdigest()[:16]
    return f'scraper.cursor.{source}.{kind}.{digest}'


def load(db,source,kind):
    item=db.get(AppSetting,key(source,kind))
    return json.loads(item.value) if item and item.value else {}


def save(db,source,kind,state):
    name=key(source,kind)
    item=db.get(AppSetting,name)
    if item is None:item=AppSetting(key=name);db.add(item)
    item.value=json.dumps(state,ensure_ascii=False)
