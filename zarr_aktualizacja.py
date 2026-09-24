# -*- coding: utf-8 -*-
'''
AKTUALIZACJA PRZYROSTOWA STORE'U ZARR (jeden rok = jedna warstwa)

Uzywane z geo_nmt.py po pobraniu i konwersji arkuszy roku:

    plan = zaplanuj(rekord_z_manifestu, arkusze_ze_skorowidza)      #PRZED pobieraniem
    ...pobieranie + process_data() -> lista GeoTIFF-ow...
    aktualizuj_rok_zarr(sciezka_zarr, geometria, rok, arkusze, tiffs, plan, ustawienia, ...)

Porownanie skorowidza z manifestem (tozsamosc arkusza = nazwa pliku + data aktualizacji):
  - rok nie ma jeszcze warstwy               -> 'nowa'      (dopisuje warstwe, time rosnie o 1)
  - doszly/zmienily sie/zniknely arkusze     -> 'zmiana'    (przebudowa TYLKO dotknietych blokow)
  - nic sie nie zmienilo                     -> 'aktualna'  (rok pomijany, nic sie nie pobiera)
  - poprzedni zapis przerwany ('w_toku')     -> 'przebudowa' (cala warstwa od nowa)

GUGiK nie wysyla powiadomien o nowych danych - geo_nmt.py trzeba uruchamiac cyklicznie.
'''

import os
import zipfile

import numpy as np
import rasterio

from nfp_mosaics import find_date
from nfp_mosaics_zarr import siatka_z_geometrii, zbuduj_warstwe_bazowa
from zarr_magazyn import Magazyn, waliduj_poziomy, ZMIENNA_ROZDZIELCZOSCI
from zarr_poziomy import przelicz_poziom, nazwa_metody

ROZSZERZENIA_ASCII = ('.asc', '.xyz', '.txt')


def _klucz(a):
    #tozsamosc arkusza: ten sam plik z ta sama data aktualizacji = ta sama dana
    return (a['plik'], a.get('akt_data') or '')


def _opis_arkusza(a):
    #tylko to, co trafia do manifestu (proste typy, bez geometrii)
    return {'plik': a['plik'], 'akt_data': a.get('akt_data') or '', 'godlo': a.get('godlo', ''),
            'nr_zglosz': a.get('nr_zglosz', ''), 'format': a.get('format', ''),
            'bounds': [float(v) for v in a['bounds']]}


def zaplanuj(rekord, arkusze, wymus=False):
    '''
    rekord  : wpis manifestu dla roku (albo None, gdy rok nie ma warstwy)
    arkusze : arkusze roku ze skorowidza - lista slownikow {'plik','akt_data','bounds',...}
    zwraca {'akcja': 'nowa'|'aktualna'|'zmiana'|'przebudowa',
            'brudne': lista prostokatow albo None (=caly obszar),
            'dodane': [...], 'usuniete': [...],
            'zmienione': [nazwy plikow wydanych ponownie z inna data]}
    '''
    if rekord is None:
        return {'akcja': 'nowa', 'brudne': None, 'dodane': [_klucz(a) for a in arkusze],
                'usuniete': [], 'zmienione': []}
    if wymus or rekord.get('status') != 'kompletna':
        return {'akcja': 'przebudowa', 'brudne': None, 'dodane': [], 'usuniete': [], 'zmienione': []}

    stare = {_klucz(a): a for a in rekord.get('arkusze', [])}
    bez_danych = {_klucz(a) for a in rekord.get('nieobslugiwane', [])}   #np. arkusze bez plikow ASCII
    nowe = {_klucz(a): a for a in arkusze}

    dodane = sorted(set(nowe) - set(stare) - bez_danych)
    usuniete = sorted(set(stare) - set(nowe))
    if not dodane and not usuniete:
        return {'akcja': 'aktualna', 'brudne': None, 'dodane': [], 'usuniete': [], 'zmienione': []}

    zmienione = sorted({k[0] for k in dodane} & {k[0] for k in usuniete})
    brudne = [tuple(nowe[k]['bounds']) for k in dodane] + [tuple(stare[k]['bounds']) for k in usuniete]
    return {'akcja': 'zmiana', 'brudne': brudne, 'dodane': dodane, 'usuniete': usuniete,
            'zmienione': zmienione}


def policz_udzialy(tab_res, indeks, siatka, blok_px):
    '''
    Udzial pikseli z danymi wg rozdzielczosci zrodlowej w warstwie o danym indeksie czasu, np.
    {'0.5': 0.82, '5': 0.18}. Liczone blokami z tablicy zmiennej rozdzielczosc_zrodla (po zapisie).
    '''
    licznik = {}
    for y0, y1, x0, x1 in siatka.okna(blok_px):
        blok = np.asarray(tab_res[indeks, y0:y1, x0:x1])
        blok = blok[~np.isnan(blok)]
        if blok.size:
            wart, ile = np.unique(np.round(blok, 4), return_counts=True)
            for w, n in zip(wart, ile):
                licznik[float(w)] = licznik.get(float(w), 0) + int(n)
    razem = sum(licznik.values())
    return {f'{k:g}': round(v / razem, 4) for k, v in sorted(licznik.items())} if razem else {}


def _tag(sciezka, klucz):
    with rasterio.open(sciezka) as src:
        return src.tags().get(klucz)


def _zip_bez_ascii(sciezka):
    #True, jesli archiwum nie zawiera zadnego pliku ASCII/XYZ (np. arkusz w formacie GeoTIFF)
    try:
        with zipfile.ZipFile(sciezka) as z:
            return not any(n.lower().endswith(ROZSZERZENIA_ASCII) for n in z.namelist())
    except Exception:
        return False


def aktualizuj_rok_zarr(sciezka_zarr, geometria, rok, arkusze, tiffs, plan, ustawienia,
                        nazwa_powiatu='', folder_wejsciowy=None):
    '''
    sciezka_zarr     : store (tworzony, jesli nie istnieje)
    geometria        : granica powiatu w EPSG:2180 (shapely)
    arkusze          : arkusze roku ze skorowidza (lista slownikow)
    tiffs            : lista GeoTIFF-ow z processor.process_data (z tagami plik_zrodlowy, akt_data)
    plan             : wynik zaplanuj()
    ustawienia       : {'rozdzielczosc_bazowa': liczba|None, 'poziomy': [5, 10, 20],
                        'resampling_poziomow': 'bilinear', 'blok_px': 2048, 'zarr_format': 2}
    folder_wejsciowy : folder z pobranymi archiwami (do rozpoznania arkuszy bez plikow ASCII)
    zwraca {'akcja': ..., 'indeks': ..., 'bloki': ..., 'arkusze': ...}
    '''
    blok_px = int(ustawienia.get('blok_px', 2048))
    mag = Magazyn(sciezka_zarr, ustawienia.get('zarr_format', 2), blok_px)

    #---KTORE TIFF-Y NALEZA DO BIEZACEJ LISTY ARKUSZY (folder wejsciowy moze zawierac stare pliki)---
    znane = {a['plik'] for a in arkusze}
    kafle = []
    obce = 0
    for p in tiffs:
        if _tag(p, 'plik_zrodlowy') in znane:
            kafle.append(p)
        else:
            obce += 1
    if obce:
        print(f'[ZARR] Pominieto {obce} GeoTIFF-ow, ktore nie pochodza z biezacej listy arkuszy '
              f'(np. arkusz zniknal ze skorowidza).')

    uzyte_pliki = {_tag(p, 'plik_zrodlowy') for p in kafle}
    uzyte = [a for a in arkusze if a['plik'] in uzyte_pliki]
    nieobslugiwane, pominiete = [], []
    for a in arkusze:
        if a['plik'] in uzyte_pliki:
            continue
        sciezka = os.path.join(folder_wejsciowy, a['plik']) if folder_wejsciowy else None
        if sciezka and os.path.exists(sciezka) and _zip_bez_ascii(sciezka):
            nieobslugiwane.append(a)          #brak ASCII/XYZ w archiwum - nie ma czego konwertowac
        else:
            pominiete.append(a['plik'])       #brak pliku / blad konwersji - zostanie ponowione
    if nieobslugiwane:
        print(f'[ZARR] {len(nieobslugiwane)} arkuszy nie zawiera plikow ASCII/XYZ (np. inny format) - '
              f'zapisane jako nieobslugiwane.')
    if pominiete:
        print(f'[UWAGA] Pominieto {len(pominiete)} arkuszy (brak pliku albo blad konwersji) - zostana '
              f'ponowione przy nastepnym uruchomieniu: {pominiete}')

    if not kafle:
        print(f'[BLAD] Rok {rok}: zaden arkusz nie zostal przygotowany - nic nie zapisuje.')
        return {'akcja': 'blad'}

    res_kafli = {}
    for p in kafle:
        with rasterio.open(p) as src:
            res_kafli[p] = round(abs(src.res[0]), 2)

    #---TWORZENIE STORE'U (ZAMROZENIE SIATKI) ALBO SPRAWDZENIE ZGODNOSCI---
    metoda = nazwa_metody(ustawienia.get('resampling_poziomow', 'bilinear'))
    if not mag.istnieje():
        bazowa = ustawienia.get('rozdzielczosc_bazowa') or min(res_kafli.values())
        faktory = waliduj_poziomy(bazowa, ustawienia.get('poziomy', []))
        siatka = siatka_z_geometrii(geometria, bazowa, faktory)
        mag.utworz(siatka, faktory, meta={'powiat': nazwa_powiatu, 'resampling_poziomow': metoda})
        if not ustawienia.get('rozdzielczosc_bazowa'):
            print(f'[UWAGA] Rozdzielczosc bazowa ustalona automatycznie na {bazowa} m (najdrobniejszy piksel) '
                  f'i ZAMROZONA. Aby ja narzucic, wpisz "rozdzielczosc" w config.json.')
    siatka = mag.siatka()
    opis = mag.opis()
    w_store = [round(siatka.cellsize * f, 6) for f in mag.faktory()]
    if sorted(float(x) for x in ustawienia.get('poziomy', [])) != w_store:
        print(f"[UWAGA] Poziomy w config.json ({ustawienia.get('poziomy', [])}) roznia sie od poziomow "
              f"store'u ({w_store}). Poziomy sa ZAMROZONE przy tworzeniu store'u - uzywam poziomow ze store'u.")
    bazowa_cfg = ustawienia.get('rozdzielczosc_bazowa')
    if bazowa_cfg and abs(float(bazowa_cfg) - siatka.cellsize) > 1e-9:
        print(f"[UWAGA] Rozdzielczosc w config.json ({bazowa_cfg} m) rozni sie od store'u ({siatka.cellsize} m) "
              f"- uzywam wartosci ze store'u.")
    metoda = opis.get('resampling_poziomow', metoda)

    grubsze = sum(1 for r in res_kafli.values() if r > siatka.cellsize + 1e-6)
    drobniejsze = sum(1 for r in res_kafli.values() if r < siatka.cellsize - 1e-6)
    if grubsze:
        print(f'[ZARR] {grubsze} arkuszy ma piksel grubszy niz baza ({siatka.cellsize} m) - sa probkowane '
              f'do siatki bazowej (nie zawieraja prawdziwego szczegolu).')
    if drobniejsze:
        print(f'[UWAGA] {drobniejsze} arkuszy ma piksel drobniejszy niz baza ({siatka.cellsize} m) - '
              f'szczegol jest tracony.')

    #---WARSTWA: STATUS 'w_toku' -> ZAPIS -> 'kompletna'---
    idx = mag.przygotuj_warstwe(rok)
    skladniki = [_opis_arkusza(a) for a in uzyte]
    rozdz = {}
    for r in res_kafli.values():
        rozdz[f'{r:g}'] = rozdz.get(f'{r:g}', 0) + 1
    mag.zapisz_rekord(rok, mag.rekord_nowy(idx, skladniki, 'w_toku', rozdzielczosci_zrodlowe=rozdz))

    grupy = mag.grupy()
    tab_baza = mag.tablica(grupy[0][0])
    posortowane = sorted(kafle, key=lambda p: (find_date(p, None), p), reverse=True)   #najnowsze pierwsze
    tab_baza_res = mag.tablica(grupy[0][0], ZMIENNA_ROZDZIELCZOSCI)
    zapisane = zbuduj_warstwe_bazowa(tab_baza, siatka, geometria, posortowane, idx, blok_px,
                                     brudne=plan['brudne'], pelny_zapis=plan['akcja'] != 'nowa',
                                     tablica_res=tab_baza_res)
    for nazwa, faktor, siatka_poz in grupy[1:]:
        przelicz_poziom(tab_baza, mag.tablica(nazwa), siatka, siatka_poz, faktor, idx,
                        mag.blok_poziomu(faktor), metoda=metoda, brudne=plan['brudne'],
                        tab_baza_res=tab_baza_res, tab_poziom_res=mag.tablica(nazwa, ZMIENNA_ROZDZIELCZOSCI))
    udzialy = policz_udzialy(tab_baza_res, idx, siatka, blok_px)

    mag.zapisz_rekord(rok, mag.rekord_nowy(
        idx, skladniki, 'kompletna', rozdzielczosci_zrodlowe=rozdz, udzial_rozdzielczosci=udzialy,
        pominiete=pominiete, nieobslugiwane=[_opis_arkusza(a) for a in nieobslugiwane]))
    print(f'[ZARR] Rok {rok} gotowy (indeks czasu {idx}, zapisanych blokow bazy: {zapisane}).')
    return {'akcja': plan['akcja'], 'indeks': idx, 'bloki': zapisane, 'arkusze': len(uzyte)}
