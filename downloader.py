import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed


def download_single_file(url_input, target_dir):
    #---POBIERA POJEDYNCZY NMT Z URL---
    try:
        #wyciagam ze slownika tylko url (jak jest)
        if isinstance(url_input, dict):
            url = url_input.get('url')
        else:
            url = url_input
        file_name = url.split('/')[-1]
        file_path = os.path.join(target_dir, file_name)

        if os.path.exists(file_path):
            #jesli plik ma wagę >5 KB, to blad HTML, pobieram od nowa
            if os.path.getsize(file_path) > 5000:
                return f"[DOWNLOADER] Pominieto (istnieje): {file_name}"

        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://geoportal.gov.pl/'}

        response = requests.get(url, headers=headers, stream=True, timeout=30)
        
        if response.status_code == 200:
            if 'text/html' in response.headers.get('Content-Type', ''):
                return f"[DOWNLOADER] Blad serwera: {file_name}"
                
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return f"[DOWNLOADER] Pobrano: {file_name} ({round(os.path.getsize(file_path)/(1024*1024), 2)} MB)"
        else:
            return f"[DOWNLOADER] Blad serwera {response.status_code}: {file_name}"
    except Exception as e:
        return f"[DOWNLOADER] Blad podczas pobierania {url}: {e}"


def download_nmt_files(links, target_dir, max_workers=3):
    #---PRZETWARZA LISTE LINKOW I JE POBIERA---
    if not os.path.exists(target_dir):
        os.makedirs(target_dir)

    print(f"[DOWNLOADER] Rozpoczynanie pobierania {len(links)} plikow")
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(download_single_file, url, target_dir) for url in links]
        for future in as_completed(futures):
            result = future.result()
            print(result)
            
    print(f"[DOWNLOADER] Zakonczono pobieranie plikow do: {target_dir}")
