# -*- coding: utf-8 -*-
'''
PODKLADY MAPY HTML (folium)

Dlaczego nie 'CartoDB positron': od konca sierpnia 2026 CARTO wymaga klucza API dla podkladow rastrowych
(basemaps.cartocdn.com); bez klucza kafelki maja znak wodny "API KEY REQUIRED". Skrot 'CartoDB positron'
w folium nie ma miejsca na klucz.

Wszystkie podklady dodawane sa jako warstwy do wyboru w panelu warstw (LayerControl); domyslnie
widoczny jest ten z config.json ('mapa': {'podklad': ...}):
    'esri'           Esri World Street Map     (bez klucza)
    'esri_satelita'  Esri World Imagery        (bez klucza)
    'osm'            OpenStreetMap             (bez klucza; polityka OSM ogranicza duzy ruch)
    'carto'          CARTO Voyager             (wymaga bezplatnego klucza: carto.com/basemaps/apikey,
                                                wpisz go jako 'carto_key' w config.json)
'''

import folium

PODKLADY = {
    'esri': ('Mapa (Esri)',
             'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
             'Tiles &copy; Esri &mdash; Source: Esri, DeLorme, NAVTEQ, USGS, and others', 19),
    'esri_satelita': ('Zdjecia satelitarne (Esri)',
                      'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
                      'Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and others', 19),
    'osm': ('OpenStreetMap',
            'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
            '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', 19),
}
URL_CARTO = 'https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png?key={klucz}'
ATRYBUCJA_CARTO = ('&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>, '
                   '&copy; <a href="https://carto.com/attributions">CARTO</a>')


def dodaj_podklady(mapa, domyslny='esri', carto_key=None):
    '''
    dodaje podklady do mapy folium utworzonej z tiles=None; zwraca liste nazw dodanych podkladow.
    Jesli wybrano 'carto' bez klucza, uzywa 'esri' (i ostrzega), zeby nie pokazywac znaku wodnego.
    '''
    domyslny = str(domyslny or 'esri').strip().lower()
    if domyslny == 'carto' and not carto_key:
        print("[UWAGA] Podklad 'carto' wymaga klucza (bezplatny: https://carto.com/basemaps/apikey) - "
              "wpisz go jako 'carto_key' w sekcji 'mapa' w config.json. Uzywam podkladu 'esri'.")
        domyslny = 'esri'
    if domyslny not in PODKLADY and domyslny != 'carto':
        print(f"[UWAGA] Nieznany podklad '{domyslny}' (dostepne: {sorted(PODKLADY) + ['carto']}) - uzywam 'esri'.")
        domyslny = 'esri'

    dodane = []
    for klucz, (nazwa, url, atrybucja, maxzoom) in PODKLADY.items():
        folium.TileLayer(tiles=url, attr=atrybucja, name=nazwa, overlay=False, control=True,
                         max_zoom=maxzoom, show=(klucz == domyslny)).add_to(mapa)
        dodane.append(klucz)
    if carto_key:
        folium.TileLayer(tiles=URL_CARTO.replace('{klucz}', str(carto_key)), attr=ATRYBUCJA_CARTO,
                         name='CARTO Voyager', overlay=False, control=True, max_zoom=20,
                         subdomains='abcd', show=(domyslny == 'carto')).add_to(mapa)
        dodane.append('carto')
    return dodane
