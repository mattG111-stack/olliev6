"""Parse only URL-bound HouGarden property facts."""
import json
import re
from datetime import datetime, timezone
from urllib.parse import urldefrag
from html.parser import HTMLParser

class Node:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children, self.parts = tag, dict(attrs), [], []
    def text(self):
        return ''.join(p.text() if isinstance(p, Node) else p for p in self.parts)
    def all(self, tag):
        return [n for c in self.children for n in ([c] if c.tag == tag else []) + c.all(tag)]

class Document(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)
    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs)
        self.stack[-1].children.append(n)
        self.stack[-1].parts.append(n)
        if tag not in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}:
            self.stack.append(n)
    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack = self.stack[:i]
                break
    def handle_data(self, data):
        self.stack[-1].parts.append(data)


def parse(html, url):
    soup = Document(html).root
    nodes = []
    for script in [n for n in soup.all('script') if n.attrs.get('type') == 'application/ld+json']:
        try:
            value = json.loads(script.text())
        except (ValueError, TypeError):
            continue
        for node in value if isinstance(value, list) else [value]:
            if isinstance(node, dict):
                nodes.extend(node.get('@graph', [node]))
    matches = [n for n in nodes if isinstance(n, dict) and urldefrag(n.get('url') or n.get('@id') or '')[0].rstrip('/') == url.rstrip('/') and isinstance(n.get('address'), dict)]
    if len(matches) != 1:
        return None
    residence = matches[0]
    address = residence['address']
    if not address.get('streetAddress') or not address.get('addressLocality'):
        return None
    item = dict(_apex_direct=True, source='hougarden', url=url, address=address['streetAddress'], suburb=address['addressLocality'], scraped_at=datetime.now(timezone.utc).isoformat())
    def number(value):
        match = re.fullmatch(r'\s*([\d,]+(?:\.\d+)?)\s*(?:m²|m2)?\s*', str(value))
        return float(match[1].replace(',', '')) if match else None
    for field, key in [('beds','numberOfBedrooms'), ('baths','numberOfBathroomsTotal')]:
        value = number(residence.get(key))
        if value and value <= 30:
            item[field] = value
    size = residence.get('floorSize') or {}
    if isinstance(size, dict) and size.get('unitCode') == 'MTK':
        item['floor_area_m2'] = number(size.get('value'))
    facts = {}
    sections = [n for n in soup.all('section') if any(h.text().strip() == 'Key Facts' for h in n.all('h3'))]
    if len(sections) == 1:
        for div in sections[0].all('div'):
            spans = [n for n in div.children if n.tag == 'span']
            if len(spans) == 2:
                facts[spans[0].text().strip()] = spans[1].text().strip()
    for label, field in [('Floor area','floor_area_m2'),('Land area','land_area_m2'),('Parking','carspaces')]:
        value = number(facts.get(label))
        if value and value > 0:
            if item.get(field) and item[field] != value:
                item.setdefault('source_conflicts', {})[field] = [item[field],value]
            else:
                item[field] = value
    for label, field in [('Type of title','type_of_title'),('Zoning','zoning'),('Building Condition','condition'),('Decade of construction','building_age')]:
        if facts.get(label):
            item[field] = facts[label]
    image = residence.get('image')
    if isinstance(image, str) and image.startswith('https://s.hougarden.com/'):
        item['images'] = [image]
    item['source_evidence'] = {'residence':residence, 'key_facts':facts}
    return item
