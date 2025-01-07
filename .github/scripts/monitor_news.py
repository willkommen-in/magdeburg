import json
import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import os
from openai import OpenAI
import base64
import difflib

# News sources to monitor
SOURCES = [
    {
        'name': 'MDR Sachsen-Anhalt',
        'feed': 'https://www.mdr.de/nachrichten/index-rss.xml',
        'keywords': [
            'magdeburg', 
            'rassistisch', 
            'fremdenfeindlich',
            'ausländerfeindlich',
            'hassverbrechen',
            'übergriff',
            'angriff migranten',
            'rassismus'
        ]
    },
    {
        'name': 'taz',
        'feed': 'https://taz.de/!p4608;rss/',
        'keywords': [
            'magdeburg',
            'rassistisch',
            'fremdenfeindlich',
            'ausländerfeindlich',
            'hassverbrechen',
            'übergriff',
            'angriff migranten',
            'rassismus'
        ]
    }
]

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

def load_current_incidents():
    with open('data/incidents.json', 'r', encoding='utf-8') as f:
        return json.load(f)

def extract_text_from_article(url):
    """Extract main article text from URL"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'de,en-US;q=0.7,en;q=0.3',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Cache-Control': 'max-age=0'
    }
    
    try:
        response = requests.get(
            url, 
            headers=headers, 
            allow_redirects=True,
            timeout=30
        )
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # MDR specific extraction
        if 'mdr.de' in url:
            article = soup.select_one('article')
            if article:
                paragraphs = article.select('p')
                return ' '.join(p.get_text() for p in paragraphs)
        
        # taz specific extraction
        if 'taz.de' in url:
            article = soup.select_one('.article')
            if article:
                paragraphs = article.select('p')
                return ' '.join(p.get_text() for p in paragraphs)
        
        print(f"Could not extract text from {url}")
        return None
        
    except requests.exceptions.TooManyRedirects:
        print(f"Too many redirects for {url}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"Error fetching {url}: {str(e)}")
        return None
    except Exception as e:
        print(f"Unexpected error processing {url}: {str(e)}")
        return None

def parse_with_llm(article_text, url, source_name):
    """Use OpenAI to parse article text into structured incident data"""
    
    prompt = f"""Analysiere diesen Artikel nach rassistisch motivierten Vorfällen in Magdeburg.
    Extrahiere nur Vorfälle, die:
    1. In Magdeburg stattgefunden haben
    2. Rassistisch oder fremdenfeindlich motiviert waren
    3. Nach dem 19. Dezember 2023 passiert sind
    
    Falls kein solcher Vorfall beschrieben wird, antworte mit "null".
    
    Formatiere jeden Vorfall als JSON mit:
    - date (YYYY-MM-DD)
    - location (Ort in Magdeburg)
    - description (kurze faktische Beschreibung)
    - sources (Array mit url und name)
    - type (physical_attack, verbal_attack, property_damage, oder other)
    - status (verified wenn von Polizei/Behörden bestätigt)

    Artikel:
    {article_text}
    """

    response = client.chat.completions.create(
        model="gpt-4-turbo-preview",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )

    try:
        result = response.choices[0].message.content
        if result.strip().lower() == "null":
            return None
        incident = json.loads(result)
        incident['sources'].append({
            'url': url,
            'name': source_name
        })
        return incident
    except:
        print(f"Failed to parse incident from {url}")
        return None

def create_pull_request(new_incidents):
    """Create a PR with new incidents"""
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    
    if not repo or not token:
        print("Missing repository information or token")
        return

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    api_base = "https://api.github.com"

    # Create a new branch
    branch_name = f"update-incidents-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    
    # Get the current main branch SHA
    r = requests.get(f"{api_base}/repos/{repo}/git/ref/heads/main", headers=headers)
    if r.status_code != 200:
        print("Failed to get main branch reference")
        return
    main_sha = r.json()["object"]["sha"]

    # Create new branch
    data = {
        "ref": f"refs/heads/{branch_name}",
        "sha": main_sha
    }
    r = requests.post(f"{api_base}/repos/{repo}/git/refs", headers=headers, json=data)
    if r.status_code != 201:
        print("Failed to create branch")
        return

    # Update file in new branch
    with open('data/incidents.json', 'r', encoding='utf-8') as f:
        content = f.read()
    
    data = {
        "message": f"Add {len(new_incidents)} new incidents",
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch_name
    }
    
    r = requests.put(
        f"{api_base}/repos/{repo}/contents/data/incidents.json",
        headers=headers,
        json=data
    )
    
    if r.status_code != 200:
        print("Failed to update file")
        return

    # Create PR
    pr_data = {
        "title": f"Add {len(new_incidents)} new incidents",
        "body": "Automatically detected new incidents from news sources.",
        "head": branch_name,
        "base": "main"
    }
    
    r = requests.post(f"{api_base}/repos/{repo}/pulls", headers=headers, json=pr_data)
    if r.status_code != 201:
        print("Failed to create PR")
        return
    
    print(f"Created PR: {r.json()['html_url']}")

def is_duplicate(new_incident, existing_incidents):
    """Check if an incident is already recorded using GPT-4"""
    # First check exact URL matches
    for existing in existing_incidents:
        existing_urls = {source['url'] for source in existing['sources']}
        new_urls = {source['url'] for source in new_incident['sources']}
        if existing_urls & new_urls:  # If there's any overlap in URLs
            return True

    # For incidents on the same date, use GPT-4 to check if they're the same
    same_date_incidents = [
        incident for incident in existing_incidents 
        if incident['date'] == new_incident['date']
    ]
    
    if same_date_incidents:
        prompt = f"""Compare these incidents and determine if they are the same event reported differently.
        Consider location, type of attack, and description details.
        Return only "true" if they are the same incident, or "false" if different.

        Incident 1:
        Location: {new_incident['location']}
        Description: {new_incident['description']}
        Type: {new_incident['type']}

        Compare with each:
        {json.dumps([{
            'location': inc['location'],
            'description': inc['description'],
            'type': inc['type']
        } for inc in same_date_incidents], indent=2, ensure_ascii=False)}
        """

        response = client.chat.completions.create(
            model="gpt-4-turbo-preview",
            messages=[{
                "role": "user", 
                "content": prompt
            }],
            temperature=0
        )

        is_same = response.choices[0].message.content.strip().lower() == "true"
        
        if is_same:
            # Merge sources if it's the same incident
            for existing in same_date_incidents:
                existing_urls = {source['url'] for source in existing['sources']}
                existing['sources'].extend([
                    s for s in new_incident['sources'] 
                    if s['url'] not in existing_urls
                ])
            return True

    return False

def debug_feed(feed_url):
    """Debug RSS feed access"""
    print(f"\nTesting feed: {feed_url}")
    try:
        response = requests.get(
            feed_url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/rss+xml, application/xml'
            },
            allow_redirects=False  # Don't follow redirects to see what's happening
        )
        print(f"Status: {response.status_code}")
        if response.status_code == 301 or response.status_code == 302:
            print(f"Redirects to: {response.headers.get('Location')}")
        return response.status_code
    except Exception as e:
        print(f"Error: {str(e)}")
        return None

def main():
    # Check OpenAI API key first
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Error: OPENAI_API_KEY environment variable not set")
        return
    if not api_key.startswith("sk-"):
        print("Error: Invalid OpenAI API key format")
        return
        
    current_data = load_current_incidents()
    new_incidents = []
    
    # Debug feeds first
    for source in SOURCES:
        print(f"\nChecking feed: {source['feed']}")
        try:
            # Force UTF-8 encoding for feeds
            response = requests.get(source['feed'])
            response.encoding = 'utf-8'
            feed = feedparser.parse(response.text)
            
            if feed.bozo:  # feedparser's way of indicating parsing errors
                print(f"Error parsing feed: {feed.bozo_exception}")
                continue
                
            print(f"Found {len(feed.entries)} entries")
            for entry in feed.entries:
                print(f"- {entry.title}")
                if any(keyword in entry.title.lower() or 
                      keyword in getattr(entry, 'description', '').lower()
                      for keyword in source['keywords']):
                    
                    print(f"  Found matching keywords in: {entry.link}")
                    article_text = extract_text_from_article(entry.link)
                    if not article_text:
                        continue
                    
                    try:
                        incident = parse_with_llm(article_text, entry.link, source['name'])
                        if incident and not is_duplicate(incident, current_data['incidents']):
                            new_incidents.append(incident)
                    except Exception as e:
                        print(f"Error processing article {entry.link}: {str(e)}")
                        continue
                        
        except Exception as e:
            print(f"Error processing feed {source['feed']}: {str(e)}")
            continue
    
    if new_incidents:
        current_data['incidents'].extend(new_incidents)
        current_data['lastUpdated'] = datetime.utcnow().isoformat() + 'Z'
        create_pull_request(current_data)

if __name__ == '__main__':
    main() 