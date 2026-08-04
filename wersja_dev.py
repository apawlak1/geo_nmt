# -*- coding: utf-8 -*-
'''
WERSJA 'DEWELOPERSKA' - do obliczen i eksperymentow generalizacji.

Struktura pliku (w tej kolejnosci):
  1. IMPORTY
  2. WSZYSTKIE FUNKCJE POMOCNICZE (parametry rastrow, wykresy, raporty)
  3. FUNKCJE PIPELINE'U:
       przygotuj_baze_powiatu() - WFS + pobieranie + kafle natywne,
                                  WYKONYWANE RAZ NA ROK (niezalezne od
                                  rozdzielczosci eksperymentalnej)
       analizuj_rozdzielczosc() - KROK 1-6, wykonywane per (rok, rozdzielczosc)
  4. KONFIGURACJA (config_dev.json)
  5. PETLA GLOWNA: JEDEN powiat (bez petli po powiatach), petla po latach
     i po rozdzielczosciach eksperymentalnych
'''

import os
import sys
import glob
from pathlib import Path
import numpy as np
import pandas as pd
import geopandas as gpd
import requests
import rasterio
import xml.etree.ElementTree as ET
from shapely.geometry import box
import matplotlib
matplotlib.use('Agg')  # wymusza brak okien - tylko zapis do pliku
import matplotlib.pyplot as plt
from scipy.stats import norm
from processor import process_data
from analiza import analiza_wielorozdzielczosciowa, analiza_fp_generalizacji
from downloader import download_nmt_files, download_powiaty, UKLADY, read_config, strip_list
from mapa_kafli import zbuduj_mape_pochodzenia
from fp_filters import wczytaj_nmt
from nfp_mosaics import zbuduj_referencje_natywna, find_min_cellsize, find_native_cellsize
import time

start_time=time.perf_counter()

# ======================================================================
# WSZYSTKIE FUNKCJE POMOCNICZE
# ======================================================================

def _bezpieczna_nazwa(tytul):
    #---ZAMIANA TYTULU (spacje, polskie znaki, ':') NA BEZPIECZNA NAZWE PLIKU---
    zamienniki={'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z',
                  'Ą':'A','Ć':'C','Ę':'E','Ł':'L','Ń':'N','Ó':'O','Ś':'S','Ź':'Z','Ż':'Z'}
    nazwa=''.join(zamienniki.get(znak, znak) for znak in tytul)
    nazwa=''.join(znak if znak.isalnum() else '_' for znak in nazwa)
    while '__' in nazwa:
        nazwa=nazwa.replace('__', '_')
    return nazwa.strip('_')


def parametry_rastra(raster_path, tytul):
    #---PARAMETRY MATEMATYCZNE RASTRA (min/max/srednia/std). Raster juz jest
    #na dysku jako zwykly, jednopasmowy GeoTIFF - ta funkcja tylko go czyta.---
    dane, _, profil=wczytaj_nmt(raster_path)
    dane=dane.astype('float64')

    wazne=dane[~np.isnan(dane)]
    if wazne.size == 0:
        print(f'[DEV] Brak danych: {tytul}')
        return None

    stats={'plik': raster_path,
           'min': float(wazne.min()),
           'max': float(wazne.max()),
           'mean': float(wazne.mean()),
           'std': float(wazne.std()),
           'n_px': int(wazne.size)}

    print(f'[PARAMETRY] {tytul}')
    print(f"  min={stats['min']:.2f} m | max={stats['max']:.2f} m | "
          f"srednia={stats['mean']:.2f} m | std_dev={stats['std']:.2f} m")

    #---WSPOLRZEDNE PIKSELI MIN/MAX (do jednoznacznej weryfikacji w QGIS)---
    #rasterio.transform daje macierz przeksztalcenia piksel->wspolrzedne
    #(ta sama, ktora jest w profilu pliku) - row,col z np.where mnozymy
    #przez nia, zeby dostac X,Y w ukladzie wspolrzednych rastra (PL-1992).
    transform=profil['transform']
    for etykieta, wartosc_ekstremalna in (('MIN', stats['min']), ('MAX', stats['max'])):
        wiersz, kolumna=np.unravel_index(np.nanargmin(np.abs(dane - wartosc_ekstremalna)), dane.shape)
        x, y=transform * (kolumna + 0.5, wiersz + 0.5)
        print(f"  piksel {etykieta}: wiersz={wiersz}, kolumna={kolumna} "
              f"-> X={x:.2f}, Y={y:.2f} (uklad PL-1992/EPSG:2180)")

    return stats


def wykres_histogramu_roznicy(wazne, tytul, output_dir, nazwa_pliku):
    #---HISTOGRAM ROZKLADU ROZNICY (z dopasowaniem N(mu,sigma), pokazuje odchylenie
    #standardowe) - TYLKO ZAPIS DO PNG, w tym samym folderze co odpowiadajaca mu
    #mozaika. Brak wyswietlania (plt.show() celowo nie jest wywolywane).
    #nazwa_pliku to KROTKA, CZYTELNA nazwa pliku (nie generowana automatycznie
    #z pelnego, dlugiego tytulu - stad wczesniejszy balagan w nazwach).---
    mu, std=norm.fit(wazne)

    plt.figure(figsize=(8, 5))
    plt.hist(wazne, bins=100, density=True, color=(0/255, 55/255, 103/255), edgecolor='none', alpha=0.6)
    xmin, xmax=plt.xlim()
    x=np.linspace(xmin, xmax, 200)
    plt.plot(x, norm.pdf(x, mu, std), 'r-', linewidth=2,
             label=f'N($\\mu$={mu:.3f}, $\\sigma$={std:.3f})')
    plt.axvline(mu, color='black', linestyle='--', linewidth=1.2, label=f'srednia={mu:.3f}')
    plt.title(f'Rozklad roznic - {tytul}')
    plt.xlabel('Roznica wysokosci [m]')
    plt.ylabel('Gestosc prawdopodobienstwa')
    plt.legend()
    plt.tight_layout()

    sciezka_png=os.path.join(output_dir, nazwa_pliku)
    plt.savefig(sciezka_png, dpi=300, bbox_inches='tight')
    plt.close()
    print(f'[DEV] Wykres zapisany w: {sciezka_png}')

    return {'mu': float(mu), 'sigma': float(std)}


def parametry_roznicy(raster_path, tytul, stats=None, output_dir=None, nazwa_pliku=None):
    #---STATYSTYKI ROZNICY (MAE/RMSE juz policzone wczesniej przez
    #analiza_wielorozdzielczosciowa) + wykres histogramu, zapisywany do tego
    #samego folderu co mozaika (jesli podano output_dir + nazwa_pliku)---
    dane, _, _=wczytaj_nmt(raster_path)
    dane=dane.astype('float64')

    wazne=dane[~np.isnan(dane)]
    if wazne.size == 0:
        print(f'[DEV] Brak danych: {tytul}')
        return None

    if stats:
        print(f'[PARAMETRY] {tytul}')
        print(f"  n={stats.get('n_px')} | MAE={stats.get('MAE'):.3f} m | "
              f"RMSE={stats.get('RMSE'):.3f} m | min={stats.get('min'):.3f} m | "
              f"max={stats.get('max'):.3f} m | std={stats.get('std'):.3f} m")

    rozklad=None
    if output_dir and nazwa_pliku:
        rozklad=wykres_histogramu_roznicy(wazne, tytul, output_dir, nazwa_pliku)

    return rozklad


def parametry_roznicy_fp(dem_oryginal_path, dem_fp_path, tytul, stats=None,
                          output_dir=None, nazwa_pliku_wyniku=None):
    #---ROZNICA (ORYGINAL - PO FILTRACJI FP), liczona w pamieci (2D, zeby dalo
    #sie ja zapisac jako raster). Statystyki (MAE/RMSE) wypisywane z gotowego
    #`stats`. RASTER ROZNICOWY zapisywany jako zwykly, jednopasmowy GeoTIFF
    #(bez kolorowania), jesli podano output_dir + nazwa_pliku_wyniku.---
    dem_oryginal, _, profil=wczytaj_nmt(dem_oryginal_path)
    dem_fp, _, _=wczytaj_nmt(dem_fp_path)
    dem_oryginal=dem_oryginal.astype('float64')
    dem_fp=dem_fp.astype('float64')

    maska=~np.isnan(dem_oryginal) & ~np.isnan(dem_fp)
    roznica_2d=np.where(maska, dem_oryginal - dem_fp, np.nan)
    roznica=roznica_2d[maska]

    if roznica.size == 0:
        print(f'[DEV] Brak danych: {tytul}')
        return None

    if stats:
        print(f'[PARAMETRY FP] {tytul}')
        print(f"  n={stats.get('n_px')} | MAE={stats.get('MAE'):.3f} m | "
              f"RMSE={stats.get('RMSE'):.3f} m | min={stats.get('min'):.3f} m | "
              f"max={stats.get('max'):.3f} m | std={stats.get('std'):.3f} m")

    sciezka_raster=None
    if output_dir and nazwa_pliku_wyniku:
        sciezka_raster=os.path.join(output_dir, nazwa_pliku_wyniku)
        profil_zapis=dict(profil)
        profil_zapis.update(dtype='float32', count=1, nodata=np.nan)
        with rasterio.open(sciezka_raster, 'w', **profil_zapis) as dst:
            dst.write(roznica_2d.astype('float32'), 1)
        print(f'[DEV] Raster roznicy FP zapisany w: {sciezka_raster}')

    mu, std=norm.fit(roznica)
    return {'mu': float(mu), 'sigma': float(std), 'plik': sciezka_raster}


def pokaz_sygnatury_cva(cva_oryginal, cva_fp, tytul):
    #---POROWNANIE SYGNATUR SKALOWYCH CVA (oryginal vs kazda metoda FP) - konsola,
    #trafia tez do raportu FP przez zapisz_raport_fp()---
    print(f'\n[PARAMETRY] Sygnatura skalowa CVA: {tytul}')
    print(f"{'okno':>6} | {'oryginal':>10} | "+" | ".join(f"{n:>10}" for n in cva_fp))
    for okno in cva_oryginal:
        wiersz=f"{okno:>6} | {cva_oryginal[okno]:>10.4f} | "
        wiersz += " | ".join(f"{cva_fp[nazwa].get(okno, float('nan')):>10.4f}" for nazwa in cva_fp)
        print(wiersz)


def pokaz_mape_kafli(raster_path, mapowanie_id, tytul):
    #---MAPA POCHODZENIA JUZ ZAPISANA (surowe ID, bez kolorowania) PRZEZ zbuduj_mape_pochodzenia
    #---tu tylko wypisanie legendy ID -> plik zrodlowy (kolorowanie recznie w QGIS)
    print(f'[PARAMETRY] {tytul} - mapowanie ID -> plik zrodlowy:')
    print(f'  Raster zapisany w: {raster_path}')
    for id_kafla, nazwa in mapowanie_id.items():
        print(f'  {id_kafla}: {nazwa}')


def zapisz_raport_nfp(output_dir, powiat_save, year, rozdzielczosc, cellsize_natywna,
                       cellsize_eksperymentalna, stats_natywna, stats_nfp):
    #---RAPORT: MOZAIKA NATYWNA + METODY NFP (nearest/bilinear/bicubic)---
    linie=[]
    linie.append(f'RAPORT NFP - GENERALIZACJA NMT: {powiat_save}_{year}_{rozdzielczosc}m')
    linie.append('-'*60)
    linie.append(f'Rozdzielczosc natywna (referencja):  {cellsize_natywna} m')
    linie.append(f'Rozdzielczosc eksperymentalna:       {cellsize_eksperymentalna} m')
    linie.append('')

    if stats_natywna:
        linie.append('---MOZAIKA NATYWNA (REFERENCYJNA, NEAREST)---')
        linie.append(f"plik: {stats_natywna['plik']}")
        linie.append(f"liczba pikseli (n_px): {stats_natywna['n_px']}")
        linie.append(f"min    ={stats_natywna['min']:.3f} m")
        linie.append(f"max    ={stats_natywna['max']:.3f} m")
        linie.append(f"srednia={stats_natywna['mean']:.3f} m")
        linie.append(f"std_dev={stats_natywna['std']:.3f} m")
        linie.append('')

    if stats_nfp:
        linie.append('---METODY NFP (interpolacja mozaiki eksperymentalnej vs referencja natywna)---')
        for nazwa, s in stats_nfp.items():
            linie.append(f'---METODA {nazwa.upper()}---')
            linie.append(f"plik: {s.get('plik', '-')}")
            linie.append(f"liczba pikseli (n_px): {s.get('n_px')}")
            linie.append(f"MAE ={s.get('MAE'):.3f} m")
            linie.append(f"RMSE={s.get('RMSE'):.3f} m")
            linie.append(f"min ={s.get('min'):.3f} m")
            linie.append(f"max ={s.get('max'):.3f} m")
            linie.append(f"std ={s.get('std'):.3f} m")
            linie.append('')

        metody_z_rmse={nazwa: s['RMSE'] for nazwa, s in stats_nfp.items() if s.get('RMSE') is not None}
        if metody_z_rmse:
            najlepsza=min(metody_z_rmse, key=metody_z_rmse.get)
            linie.append(f'Metoda NFP z najnizszym RMSE: {najlepsza} (RMSE={metody_z_rmse[najlepsza]:.3f} m)')
            linie.append('UWAGA: metoda uzywana jako baza dla dalszej analizy FP jest ustawiona na '
                         'sztywno na "nearest" (patrz KROK 4), NIEZALEZNIE od tego, ktora metoda '
                         'ma tu najnizsze RMSE.')
            linie.append('')

    tresc='\n'.join(linie)
    print('\n'+tresc)

    sciezka_raportu=os.path.join(output_dir, f'{powiat_save}_{year}_{rozdzielczosc}m_RAPORT_NFP.txt')
    with open(sciezka_raportu, 'w', encoding='utf-8') as f:
        f.write(tresc)
    print(f'[DEV] Raport NFP zapisany w: {sciezka_raportu}')
    return sciezka_raportu


def zapisz_raport_fp(output_dir, powiat_save, year, rozdzielczosc, cellsize_eksperymentalna,
                      metoda_bazowa, stats_fp, cva_oryginal, cva_fp,
                      liczba_plikow, formaty_plikow, liczba_wykorzystanych,
                      min_px_natywny, max_px_natywny):
    #---RAPORT: DANE WEJSCIOWE (ARKUSZE) + METODY FEATURE-PRESERVING + CVA---
    #
    #NOWE PARAMETRY (wzgledem poprzedniej wersji):
    #  liczba_plikow        - int, wszystkie arkusze NMT znalezione/skonwertowane
    #                         dla tego roku (np. len(tiffs_to_mosaic))
    #  formaty_plikow       - dict {nazwa_formatu: liczba}, np.
    #                         {'ASCII NMT': 40, 'ASCII XYZ': 15, 'ASCII TBD': 3}
    #                         (np. skorowidze['format'].value_counts().to_dict())
    #  liczba_wykorzystanych- int, ile arkuszy faktycznie trafilo do mozaiki
    #                         (np. len(uzyte_kafle_podstawowe))
    #  min_px_natywny       - float, najmniejszy (najdrobniejszy) piksel wsrod
    #                         arkuszy natywnych (np. find_min_cellsize(tiffs_to_mosaic)
    #                         z nfp_mosaics.py)
    #  max_px_natywny       - float, najwiekszy (najgrubszy) piksel wsrod
    #                         arkuszy natywnych (np. find_native_cellsize(tiffs_to_mosaic)
    #                         z nfp_mosaics.py)
    linie=[]
    linie.append(f'RAPORT FP - GENERALIZACJA NMT: {powiat_save}_{year}_{rozdzielczosc}m')
    linie.append('-'*60)
    linie.append(f'Rozdzielczosc eksperymentalna: {cellsize_eksperymentalna} m')
    linie.append(f'Mozaika bazowa dla filtracji FP: {metoda_bazowa}')
    linie.append('')

    linie.append('---DANE WEJSCIOWE (ARKUSZE NMT)---')
    linie.append(f'liczba plikow (wszystkie arkusze):   {liczba_plikow}')
    if formaty_plikow:
        for nazwa_formatu, liczba in formaty_plikow.items():
            linie.append(f'  {nazwa_formatu}: {liczba}')
    linie.append(f'wykorzystano w mozaice:               {liczba_wykorzystanych} z {liczba_plikow}')
    linie.append(f'najmniejszy piksel wsrod natywnych:   {min_px_natywny} m')
    linie.append(f'najwiekszy piksel wsrod natywnych:    {max_px_natywny} m')
    linie.append('')

    if stats_fp:
        linie.append('---METODY FP (feature-preserving)---')
        for nazwa, s in stats_fp.items():
            linie.append(f'---METODA {nazwa.upper()}---')
            linie.append(f"plik: {s.get('plik', '-')}")
            linie.append(f"liczba pikseli (n_px): {s.get('n_px')}")
            linie.append(f"MAE ={s.get('MAE'):.3f} m")
            linie.append(f"RMSE={s.get('RMSE'):.3f} m")
            linie.append(f"min ={s.get('min'):.3f} m")
            linie.append(f"max ={s.get('max'):.3f} m")
            linie.append(f"std ={s.get('std'):.3f} m")
            linie.append('')

        metody_z_rmse={nazwa: s['RMSE'] for nazwa, s in stats_fp.items() if s.get('RMSE') is not None}
        if metody_z_rmse:
            najlepsza=min(metody_z_rmse, key=metody_z_rmse.get)
            linie.append(f'Metoda FP z najnizszym RMSE w tym przebiegu: {najlepsza} '
                         f'(RMSE={metody_z_rmse[najlepsza]:.3f} m)')
            linie.append('')

    if cva_oryginal and cva_fp:
        linie.append('---SYGNATURY SKALOWE CVA (oryginal vs metody FP)---')
        naglowek=f"{'okno':>6} | {'oryginal':>10} | " + " | ".join(f"{n:>10}" for n in cva_fp)
        linie.append(naglowek)
        for okno in cva_oryginal:
            wiersz=f"{okno:>6} | {cva_oryginal[okno]:>10.4f} | "
            wiersz += " | ".join(f"{cva_fp[nazwa].get(okno, float('nan')):>10.4f}" for nazwa in cva_fp)
            linie.append(wiersz)
        linie.append('')

    tresc='\n'.join(linie)
    print('\n'+tresc)

    sciezka_raportu=os.path.join(output_dir, f'{powiat_save}_{year}_{rozdzielczosc}m_RAPORT_FP.txt')
    with open(sciezka_raportu, 'w', encoding='utf-8') as f:
        f.write(tresc)
    print(f'[DEV] Raport FP zapisany w: {sciezka_raportu}')
    return sciezka_raportu


#---FUNKCJE GLOWNE---

def przygotuj_baze_powiatu(nazwa_user, year_user, powiaty, cache_dir, wfs_nmt, headers):
    #---WYKONYWANE RAZ NA POWIAT+ROK: dopasowanie powiatu, zapytanie WFS,
    #pobranie arkuszy, konwersja+reprojekcja+mozaika podstawowa. NIEZALEZNE
    #od rozdzielczosci eksperymentalnej - stad odseparowane od dalszej
    #analizy, zeby przy petli po wielu rozdzielczosciach NIE pobierac tych
    #samych danych po kilka razy.
    #
    #Zwraca: (powiat_save, geometry, dane_bazowe) gdzie dane_bazowe to slownik
    #{rok: {...wszystko potrzebne do analizy w dowolnej rozdzielczosci...}}
    #albo (None, None, {}) jesli powiat/dane nie istnieja.
    powiat_test=powiaty[powiaty['JPT_NAZWA_'].str.contains(rf'\b{nazwa_user}\b', case=False, regex=True)].copy()

    if powiat_test.empty:
        print(f'[DEV] Powiat {nazwa_user} nie istnieje.')
        return None, None, {}

    powiat_save=powiat_test['JPT_NAZWA_'].iloc[0].replace(' ', '_')

    minx, miny, maxx, maxy=powiat_test.total_bounds
    geometry=powiat_test.to_crs(epsg=2180).geometry.iloc[0]

    #---ZAPYTANIE WFS O SKOROWIDZ NMT+FILTROWANIE ZBIORU NMT---
    dane_do_pobrania={}

    for year in year_user:
        print(f'\n[DEV] Pobieranie danych dla roku {year}')
        layer_name=f'gugik:SkorowidzNMT{year}'

        params_nmt={'service': 'WFS', 'version': '1.0.0', 'request': 'GetFeature',
                      'typeName': layer_name, 'outputFormat': 'text/xml; subType=gml/3.1.1',
                      'bbox': f'{minx},{miny},{maxx},{maxy}'}

        try:
            response=requests.get(wfs_nmt, params=params_nmt, headers=headers, timeout=60)
            if response.status_code != 200:
                print(f'[DEV] Serwer zwrocil kod bledu: {response.status_code}')
                continue

            root=ET.fromstring(response.content)
            features_data=[]

            for entry in root.iter():
                if 'SkorowidzNMT' in entry.tag:
                    data={}
                    for child in entry:
                        clean_tag=child.tag.split('}')[-1]
                        if child.text and child.text.strip():
                            data[clean_tag]=child.text

                    lc, uc=None, None
                    for sub in entry.iter():
                        if 'lowerCorner' in sub.tag: lc=sub.text.split()
                        elif 'upperCorner' in sub.tag: uc=sub.text.split()

                    if lc and uc:
                        data['geometry']=box(float(lc[0]), float(lc[1]), float(uc[0]), float(uc[1]))
                        features_data.append(data)

            if not features_data:
                print(f'[DEV] Brak arkuszy NMT dla roku {year} w tym obszarze')
                continue

            skorowidze_kwadrat=gpd.GeoDataFrame(features_data, crs='EPSG:2180')
            skorowidze=gpd.sjoin(skorowidze_kwadrat, powiat_test, predicate='intersects')

            if skorowidze.empty:
                print(f'[DEV] Po filtracji brak arkuszy dla roku {year}')
                continue

            #---WYKLUCZENIE FORMATU ASCII TBD+NIEROZPOZNANYCH UKLADOW---
            tbd_pominiete=0
            pominieto_uklad=0
            ldp=[]
            for _, row in skorowidze.iterrows():
                format_arkusza=str(row.get('format', '')).strip().upper()
                if 'TBD' in format_arkusza:
                    tbd_pominiete += 1
                    continue

                uklad=str(row.get('uklad_xy', '')).strip()
                if uklad not in UKLADY:
                    pominieto_uklad += 1
                    print(f"[UWAGA] Arkusz {row.get('godlo', '?')} (zgloszenie {row.get('nr_zglosz', '?')}) "
                          f"ma nierozpoznany uklad wspolrzednych ('{uklad}'). Pominieto.")
                    continue

                epsg=UKLADY[uklad]
                ldp.append({'url': row['url_do_pobrania'], 'epsg': epsg})

            if tbd_pominiete:
                print(f'[DEV] Pominieto {tbd_pominiete} arkuszy w formacie ASCII TBD')
            if pominieto_uklad:
                print(f'[DEV] Pominieto {pominieto_uklad} arkuszy z nierozpoznanym ukladem wspolrzednych')

            if ldp:
                dane_do_pobrania[year]={'linki': ldp,
                                        'folder': os.path.join(cache_dir, f'nmt_{year}_{powiat_save}'),
                                        'skorowidze': skorowidze,}

            print(f'[DEV] Znaleziono {len(skorowidze)} arkuszy dla roku {year}')

        except Exception as e:
            print(f'[DEV] Blad podczas przetwarzania roku {year}: {e}')

    if not dane_do_pobrania:
        print(f'\n[DEV] Powiat {nazwa_user}: brak danych do pobrania.')
        return powiat_save, geometry, {}

    #---POBIERANIE + KONWERSJA/REPROJEKCJA/MOZAIKA PODSTAWOWA DLA KAZDEGO ROKU---
    dane_bazowe={}

    for year, info in dane_do_pobrania.items():
        liczba=len(info['linki'])
        skorowidze=info['skorowidze']

        main_dir_yr=os.path.join(cache_dir, f'nmt_{year}_{powiat_save}')
        dir_entry=os.path.join(main_dir_yr, 'dane_wejsciowe')
        dir_2000=os.path.join(main_dir_yr, 'tiff_pl2000')
        dir_1992=os.path.join(main_dir_yr, 'tiff_pl1992')

        for d in [dir_entry, dir_2000, dir_1992]:
            os.makedirs(d, exist_ok=True)

        print(f'\n[DEV] ---POBIERANIE DANYCH DLA ROKU {year} ({liczba} arkuszy)---=')
        download_nmt_files(info['linki'], dir_entry)

        print(f'[DEV] ---KONWERSJA + REPROJEKCJA + MOZAIKA PODSTAWOWA DLA ROKU {year}---')

        mapa_ukladow={p['url'].split('/')[-1]: p['epsg'] for p in info['linki']}
        mapa_daty={row['url_do_pobrania'].split('/')[-1]: pd.to_datetime(row['akt_data'])
                     for _, row in skorowidze.iterrows()}

        nazwa_pliku_wyniku=f'NMT_{powiat_save}_{year}_FINAL.tif'
        pelna_sciezka_wyniku=os.path.join(info['folder'], nazwa_pliku_wyniku)

        wyniki_nfp_podstawowe, uzyte_kafle_podstawowe, baza_natywna_podstawowa=process_data(
            dir_entry, pelna_sciezka_wyniku, geometry, mapa_ukladow, mapa_daty,
            create_mosaic=True, extract=True, dir_a=dir_2000, dir_b=dir_1992,
            return_kafle_info=True)

        print(f'[DEV] Mozaiki podstawowe (nearest/bilinear/bicubic): {wyniki_nfp_podstawowe}')

        #---ZABEZPIECZENIE: process_data() MOGLO SIE NIE POWIESC---
        #(np. blad wewnatrz generate_nfp_mosaics - patrz pelny traceback
        #wypisywany teraz przez processor.py) - wtedy uzyte_kafle_podstawowe
        #i baza_natywna_podstawowa wychodza None. Bez tej sprawdzenia kolejny
        #krok (budowa referencji natywnej) wywalal sie niejasnym TypeError
        #('NoneType' object is not iterable) zamiast czytelnie pominac ten rok.
        if uzyte_kafle_podstawowe is None:
            print(f'[DEV] Budowa mozaik podstawowych dla roku {year} nie powiodla sie. '
                  f'Rok {year} pominieto.')
            continue

        tiffs_to_mosaic=glob.glob(os.path.join(dir_1992, '*.tif'))

        #---PRAWDZIWA REFERENCJA NATYWNA (ground truth, najdrobniejszy piksel)---
        #budowana RAZ NA ROK (nie zalezy od rozdzielczosci eksperymentalnej),
        #zeby przy petli po wielu rozdzielczosciach NIE liczyc jej za kazdym
        #razem od nowa. CELOWO NIEZALEZNA od baza_natywna_podstawowa (ta jest
        #w najgrubszym pikselu - dobra dla generalizacji, zla jako referencja
        #do bledu, patrz UWAGA 4 w nfp_mosaics.py).
        referencja_natywna_dir=os.path.join(main_dir_yr, 'referencja_natywna')
        referencja_natywna_path=os.path.join(
            referencja_natywna_dir, f'{powiat_save}_{year}_REFERENCJA_NATYWNA.tif')
        print(f'\n[DEV] ---BUDOWA REFERENCJI NATYWNEJ (ground truth) DLA ROKU {year}---')
        referencja_natywna_path, native_cellsize_referencji=zbuduj_referencje_natywna(
            uzyte_kafle_podstawowe, geometry, referencja_natywna_path)
        print(f'[DEV] Referencja natywna ({native_cellsize_referencji} m): {referencja_natywna_path}')

        dane_bazowe[year]={
            'tiffs_to_mosaic': tiffs_to_mosaic,
            'mapa_daty': mapa_daty,
            'uzyte_kafle_podstawowe': uzyte_kafle_podstawowe,
            'baza_natywna_podstawowa': baza_natywna_podstawowa,
            'referencja_natywna_path': referencja_natywna_path,
            #---DODANE: skorowidze (zawiera kolumne 'format') POTRZEBNE do
            #policzenia formaty_plikow (ASCII NMT/ASCII XYZ/...) w raporcie FP---
            'skorowidze': skorowidze,
        }

    return powiat_save, geometry, dane_bazowe


def analizuj_rozdzielczosc(rok, rozdzielczosc, dane_bazowe_rok, powiat_save, geometry, cache_dir):
    #---KROK 1-6 dla JEDNEJ pary (rok, rozdzielczosc eksperymentalna).
    #Wszystko trafia do <cache_dir>/analiza_rozdzielczosci/<powiat>_<rok>_<rozdzielczosc>m/---
    rozdzielczosc_int=int(rozdzielczosc)
    output_dir_analiza=os.path.join(
        cache_dir, 'analiza_rozdzielczosci', f'{powiat_save}_{rok}_{rozdzielczosc_int}m')
    os.makedirs(output_dir_analiza, exist_ok=True)

    print(f'\n[DEV] ---------------------------------')
    print(f'[DEV] # ANALIZA: {powiat_save} | rok {rok} | rozdzielczosc {rozdzielczosc} m')
    print(f'[DEV] # -> {output_dir_analiza}')
    print(f'[DEV] ---------------------------------')

    tiffs_to_mosaic=dane_bazowe_rok['tiffs_to_mosaic']
    mapa_daty=dane_bazowe_rok['mapa_daty']
    uzyte_kafle_podstawowe=dane_bazowe_rok['uzyte_kafle_podstawowe']
    baza_natywna_podstawowa=dane_bazowe_rok['baza_natywna_podstawowa']
    referencja_natywna_path=dane_bazowe_rok.get('referencja_natywna_path')
    skorowidze=dane_bazowe_rok.get('skorowidze')

    #---STATYSTYKI ARKUSZY WEJSCIOWYCH (liczone RAZ, uzywane w raporcie FP)---
    #liczba_plikow            - wszystkie skonwertowane/zreprojektowane kafle
    #                           natywne dla tego roku
    #formaty_plikow            - rozklad formatow wg kolumny 'format' ze
    #                           skorowidza WFS (np. ASCII NMT/ASCII XYZ/ASCII TBD)
    #liczba_wykorzystanych      - ile z tych kafli faktycznie trafilo do mozaiki
    #                           (test pokrycia JPT)
    #min_px_natywny/max_px_natywny - najdrobniejszy/najgrubszy piksel wsrod
    #                           WSZYSTKICH natywnych kafli dla tego roku
    liczba_plikow=len(tiffs_to_mosaic)
    formaty_plikow=(skorowidze['format'].value_counts().to_dict()
                     if skorowidze is not None and 'format' in skorowidze.columns else {})
    liczba_wykorzystanych=len(uzyte_kafle_podstawowe) if uzyte_kafle_podstawowe else 0
    if tiffs_to_mosaic:
        min_px_natywny=find_min_cellsize(tiffs_to_mosaic)
        max_px_natywny=find_native_cellsize(tiffs_to_mosaic)
    else:
        min_px_natywny=None
        max_px_natywny=None

    # ==================================================================
    # KROK 1 + KROK 2: MOZAIKA NATYWNA (prawdziwa referencja, zbudowana raz
    # na rok - referencja_natywna_path) + METODY NFP (uzywajace osobnej
    # bazy_natywna_podstawowa - najgrubszy piksel, patrz UWAGA 4 w
    # nfp_mosaics.py). Roznice natywna vs NFP liczone od razu, odczytujemy
    # je w KROKU 3.
    # ==================================================================
    wyniki_analizy=analiza_wielorozdzielczosciowa(
        tiffs_to_mosaic=tiffs_to_mosaic, geometry=geometry, mapa_daty=mapa_daty,
        output_dir=output_dir_analiza, base_name=f'{powiat_save}_{rok}',
        experimental_res=rozdzielczosc,
        uzyte_kafle=uzyte_kafle_podstawowe, baza_natywna=baza_natywna_podstawowa,
        referencja_natywna=referencja_natywna_path)

    sciezka_mapa_kafli, mapowanie_id=zbuduj_mape_pochodzenia(
        tiffs_to_mosaic=tiffs_to_mosaic, geometry=geometry, mapa_daty=mapa_daty,
        target_cellsize=wyniki_analizy.get('target_cellsize_natywna', 1.0),
        output_path=os.path.join(output_dir_analiza, f'{powiat_save}_{rok}_{rozdzielczosc_int}m_MAPA_KAFLI.tif'),
        uzyte_kafle=uzyte_kafle_podstawowe)

    cellsize_natywna=wyniki_analizy.get('target_cellsize_natywna')
    cellsize_eksperymentalna=wyniki_analizy.get('target_cellsize_eksperymentalna', rozdzielczosc)
    exp_res_int=int(cellsize_eksperymentalna)

    print(f'\n[DEV] ---KROK 1: MOZAIKA NATYWNA - {powiat_save} {rok} {rozdzielczosc_int}m---')
    raster_natywny=wyniki_analizy['natywna']['nearest']
    stats_natywna=parametry_rastra(
        raster_natywny, f'Mozaika referencyjna (nearest, natywna) - {powiat_save} {rok}')

    print(f'\n[DEV] ---KROK 2: METODY NFP - {powiat_save} {rok} {rozdzielczosc_int}m---')
    for nazwa, sciezka in wyniki_analizy['eksperymentalna'].items():
        parametry_rastra(sciezka, f'Mozaika {nazwa}, {cellsize_eksperymentalna} m - {powiat_save} {rok}')

    # ==================================================================
    # KROK 3: ROZNICE NATYWNA vs NFP + WYKRESY + RAPORT NFP
    # ==================================================================
    print(f'\n[DEV] ---KROK 3: ROZNICE NATYWNA vs NFP - {powiat_save} {rok} {rozdzielczosc_int}m---')
    stats_nfp={}
    for nazwa, sciezka in wyniki_analizy['eksperymentalna'].items():
        diff_path=os.path.join(
            output_dir_analiza,
            f'{powiat_save}_{rok}_ROZNICA_{nazwa}_exp{exp_res_int}m.tif')

        s=dict(wyniki_analizy['roznice'].get(nazwa, {}))
        s['plik']=sciezka
        stats_nfp[nazwa]=s

        if os.path.exists(diff_path):
            #---wykres zapisywany do tego samego folderu co mozaika NFP,
            #z krotka, czytelna nazwa (zamiast slugowania calego tytulu)---
            folder_mozaiki=os.path.dirname(sciezka) or output_dir_analiza
            nazwa_wykresu=f'{powiat_save}_{rok}_{rozdzielczosc_int}m_NFP_{nazwa.upper()}_HIST.png'
            parametry_roznicy(
                diff_path, f'Roznica {nazwa} ({cellsize_eksperymentalna} m) vs referencja - {powiat_save} {rok}',
                s, output_dir=folder_mozaiki, nazwa_pliku=nazwa_wykresu)

    zapisz_raport_nfp(output_dir_analiza, powiat_save, rok, rozdzielczosc_int,
                       cellsize_natywna, cellsize_eksperymentalna, stats_natywna, stats_nfp)

    # ==================================================================
    # KROK 4: METODY FEATURE-PRESERVING (na bazie NEAREST - juz wiadomo,
    # ze to najlepsza metoda NFP dla tego typu danych)
    # ==================================================================
    metoda_bazowa='nearest'
    referencja_fp=wyniki_analizy.get('eksperymentalna', {}).get(metoda_bazowa)

    stats_fp={}
    cva_oryginal, cva_fp={}, {}

    if referencja_fp:
        print(f'\n[DEV] ---KROK 4: FILTRACJA FEATURE-PRESERVING (baza: {metoda_bazowa}, '
              f'{cellsize_eksperymentalna} m) - {powiat_save} {rok} {rozdzielczosc_int}m---')
        wyniki_fp=analiza_fp_generalizacji(
            dem_path=referencja_fp, output_dir=output_dir_analiza,
            base_name=f'{powiat_save}_{rok}')

        # ==============================================================
        # KROK 5: RASTRY ROZNICOWE FP (zapisywane na dysk, bez kolorowania)
        # ==============================================================
        print(f'\n[DEV] ---KROK 5: RASTRY ROZNICOWE FP - {powiat_save} {rok} {rozdzielczosc_int}m---')
        for nazwa, sciezka_fp in wyniki_fp.get('wyniki_fp', {}).items():
            s_fp=dict(wyniki_fp.get('roznice_fp', {}).get(nazwa, {}))
            #---czytelna, jednolita nazwa + zapis do tego samego folderu co mozaika FP---
            folder_mozaiki_fp=os.path.dirname(sciezka_fp) or output_dir_analiza
            nazwa_pliku_diff=f'{powiat_save}_{rok}_{rozdzielczosc_int}m_FP_{nazwa.upper()}_ROZNICA.tif'

            wynik_fp=parametry_roznicy_fp(
                referencja_fp, sciezka_fp,
                f'FP {nazwa} vs {metoda_bazowa} - {powiat_save} {rok}', s_fp,
                output_dir=folder_mozaiki_fp, nazwa_pliku_wyniku=nazwa_pliku_diff)

            s_fp['plik']=sciezka_fp
            if wynik_fp:
                s_fp['plik_roznicy']=wynik_fp.get('plik')
            stats_fp[nazwa]=s_fp

        cva_oryginal=wyniki_fp.get('cva_oryginal', {})
        cva_fp=wyniki_fp.get('cva_fp', {})
        pokaz_sygnatury_cva(cva_oryginal, cva_fp, f'{powiat_save} {rok} {rozdzielczosc_int}m')

    # ==================================================================
    # KROK 6: OSOBNY RAPORT FP (+ CVA + statystyki arkuszy wejsciowych)
    # ==================================================================
    print(f'\n[DEV] ---KROK 6: RAPORT FP - {powiat_save} {rok} {rozdzielczosc_int}m---')
    zapisz_raport_fp(output_dir_analiza, powiat_save, rok, rozdzielczosc_int, cellsize_eksperymentalna,
                      metoda_bazowa, stats_fp, cva_oryginal, cva_fp,
                      liczba_plikow, formaty_plikow, liczba_wykorzystanych,
                      min_px_natywny, max_px_natywny)

    if sciezka_mapa_kafli:
        pokaz_mape_kafli(
            sciezka_mapa_kafli, mapowanie_id,
            f'Pochodzenie pikseli w mozaice - {powiat_save} {rok} {rozdzielczosc_int}m')


# ======================================================================
# KONFIGURACJA i SCIEZKI, na wlasnym config_dev
# ======================================================================
script_dir=Path(__file__).resolve().parent
config_path=script_dir / 'config_dev.json'

config_data=read_config(config_path, wymagane_klucze=('cache_dir',))
cache_dir=config_data['cache_dir']
os.makedirs(cache_dir, exist_ok=True)

wfs_nmt='https://mapy.geoportal.gov.pl/wss/service/PZGIK/NumerycznyModelTerenuEVRF2007/WFS/Skorowidze'
headers={'User-Agent': 'Mozilla/5.0'}

powiaty=download_powiaty(cache_dir)

conf_powiaty=config_data.get('powiaty')
conf_lata=config_data.get('lata')
conf_experimental_res=config_data.get('experimental_res', 20.0)

#---TYLKO JEDEN POWIAT (bez petli po powiatach) - jesli w configu jest wiecej
#niz jeden, bierzemy pierwszy i informujemy o tym---
lista_powiatow=strip_list(conf_powiaty)
if lista_powiatow:
    if len(lista_powiatow) > 1:
        print(f'[DEV] UWAGA: w config.json podano wiele powiatow ({", ".join(lista_powiatow)}), '
              f'ale ta wersja skryptu przetwarza TYLKO JEDEN powiat na uruchomienie. '
              f'Uzyty zostanie pierwszy: {lista_powiatow[0]}')
    nazwa_powiatu=lista_powiatow[0]
    print(f'[DEV] Powiat ustawiony z config.json: {nazwa_powiatu}')
else:
    print('\nBrak "powiaty" w pliku config_dev.json. Podaj nazwe powiatu:')
    nazwa_powiatu=strip_list(input())[0]

year_user=strip_list(conf_lata)
if year_user:
    print(f'[DEV] Rok (lata) ustawione z config.json: {", ".join(year_user)}')
else:
    print('\nBrak "lata" w pliku config_dev.json. '
          'Podaj rok (lub lata, oddzielone przecinkiem):')
    year_user=strip_list(input())

#---LISTA ROZDZIELCZOSCI EKSPERYMENTALNYCH - w config_dev.json "experimental_res"
#moze byc pojedyncza liczba (np. 20.0) LUB lista (np. [10.0, 20.0, 50.0])---
if isinstance(conf_experimental_res, (list, tuple)):
    lista_rozdzielczosci=[float(r) for r in conf_experimental_res]
else:
    lista_rozdzielczosci=[float(conf_experimental_res)]
print(f'[DEV] Rozdzielczosci eksperymentalne: {lista_rozdzielczosci} m')

if not nazwa_powiatu:
    print('\n[DEV] Brak powiatu do przetworzenia. ZAKONCZONO')
    sys.exit()
if not year_user:
    print('\n[DEV] Brak lat do przetworzenia. ZAKONCZONO')
    sys.exit()
if not lista_rozdzielczosci:
    print('\n[DEV] Brak rozdzielczosci do przetworzenia. ZAKONCZONO')
    sys.exit()


# ======================================================================
# GLOWNY PRZEBIEG: JEDEN powiat, petla po latach x rozdzielczosciach
# ======================================================================
print(f'\n[DEV] ---PRZYGOTOWANIE DANYCH BAZOWYCH: {nazwa_powiatu}---')
powiat_save, geometry, dane_bazowe=przygotuj_baze_powiatu(
    nazwa_powiatu, year_user, powiaty, cache_dir, wfs_nmt, headers)

if not dane_bazowe:
    print(f'\n[DEV] Brak danych bazowych dla powiatu {nazwa_powiatu}. ZAKONCZONO')
    sys.exit()

for rok in dane_bazowe:
    for rozdzielczosc in lista_rozdzielczosci:
        try:
            analizuj_rozdzielczosc(
                rok, rozdzielczosc, dane_bazowe[rok], powiat_save, geometry, cache_dir)
        except Exception as e:
            print(f'[DEV] BLAD przy analizie {powiat_save} {rok} {rozdzielczosc}m: {e}')
            continue

end_time=time.perf_counter()
execution_time=end_time-start_time
print('\n[DEV] ---ZAKONCZONO PRZETWARZANIE---')
print(f'\nSkrypt wykonano w czasie: {execution_time:.2f} sek.')