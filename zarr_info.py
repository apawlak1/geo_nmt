# -*- coding: utf-8 -*-
'''
Podsumowanie zawartosci store'u ZARR: siatka, poziomy, warstwy (lata), skladniki, statusy.

    python zarr_info.py sciezka/do/store.zarr
'''

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from zarr_magazyn import Magazyn, BladMagazynu, migruj_opis, plik_opisu


def pokrycie_lat(sciezka):
    '''
    Dla kazdego roku: jaki udzial obszaru z danymi (jakiegokolwiek roku) jest pokryty danymi TEGO roku.
    Liczone ze store'u o NAJGRUBSZYM pikselu (najszybciej). Poza granica powiatu jest NaN we wszystkich
    latach, wiec do porownania bierzemy sume pikseli z danymi ze wszystkich lat.
    Zwraca (nazwa_uzytego_store, {rok: udzial 0..1}).
    '''
    mag=Magazyn(sciezka)
    nazwa=mag.grupy()[-1][0]                       #ostatni = najgrubszy poziom (albo baza, gdy brak poziomow)
    ds=xr.open_zarr(str(mag.sciezka_grupy(nazwa)), consolidated=False)
    ma_dane=ds['nmt'].notnull()
    suma=ma_dane.any('time')
    razem=int(suma.sum().compute())
    wynik={}
    for i, t in enumerate(ds['time'].values):
        rok=int(pd.Timestamp(t).year)
        wynik[rok]=(float((ma_dane.isel(time=i) & suma).sum().compute()) / razem) if razem else 0.0
    return nazwa, wynik


def info(sciezka):
    mag=Magazyn(sciezka)
    opis=mag.opis()
    siatka=mag.siatka()
    print(f'Store:      {sciezka}')
    print(f"Powiat:     {opis.get('powiat', '?')}")
    print(f'Siatka:     {siatka.width} x {siatka.height} px, piksel {siatka.cellsize:g} m, EPSG:{siatka.epsg}')
    print(f'Granice:    {tuple(round(v, 2) for v in siatka.bounds)}')
    print('Stores (kazda rozdzielczosc = osobny .zarr, zmienna nmt w korzeniu):')
    for nazwa, faktor, s in mag.grupy():
        print(f"  {nazwa:>6}  {mag.sciezka_grupy(nazwa)}  ({s.width} x {s.height} px)"
              + ('  <- baza' if faktor == 1 else ''))
    print('Warstwy (rok = jedna warstwa czasowa):')
    for rok, rec in sorted(mag.manifest().items()):
        rozdz=', '.join(f'{r} m: {n}' for r, n in rec.get('rozdzielczosci_zrodlowe', {}).items())
        print(f"  {rok}  indeks {rec.get('indeks')}  status {rec.get('status')}  "
              f"arkuszy {len(rec.get('arkusze', []))}  piksele zrodlowe: {rozdz or '-'}  "
              f"(zmieniono {rec.get('zmieniono', '?')})")
        if rec.get('nieobslugiwane'):
            print(f"        nieobslugiwane (bez plikow ASCII/XYZ): {len(rec['nieobslugiwane'])}")
        if rec.get('pominiete'):
            print(f"        pominiete (do ponowienia): {rec['pominiete']}")

    #---CZAS I ZAWARTOSC KAZDEJ WARSTWY (czy to osobne rastry, a nie jeden z nalozonymi latami)---
    try:
        nazwa, uklad, warstwy=opis_warstw(sciezka)
        print(f"\nCzas i zawartosc warstw (poziom {nazwa}): jednostki czasu: {uklad['units']}, "
              f"granice roku (time_bnds): {'tak' if uklad['granice'] else 'nie'}, "
              f"time zapisany w fragmentach po {uklad['fragmenty_time']}")
        print('  indeks  rok   time (wartosc)        granice roku                             '
              'min     srednia    max   zmiana vs poprzednia: >0,5 cm    >0,5 m     >2 m')
        for w in warstwy:
            gr=(f"{w['granice'][0]} .. {w['granice'][1]}" if w['granice'] else '-')
            if w['zmienione'] is None:
                zm='-'
            else:
                zm=(f"{w['zmienione'] * 100:14.1f}% {w['zm_05m'] * 100:10.1f}% {w['zm_2m'] * 100:8.1f}%")
            print(f"  {w['indeks']:>6}  {w['rok']}  {w['time']}  {gr:<41}"
                  f"{w['min']:8.2f} {w['srednia']:8.2f} {w['max']:8.2f}   {zm}")
        print('  (zmiana >0,5 cm to zwykle szum i inna rozdzielczosc arkuszy; realne zmiany terenu to >0,5 m i >2 m)')
    except Exception as e:
        print(f'\n(nie udalo sie odczytac czasu warstw: {e})')

    #---ROZDZIELCZOSC ZRODLOWA PIKSELI (zmienna rozdzielczosc_zrodla): ile danych jest prawdziwym szczegolem---
    udzialy={rok: rec.get('udzial_rozdzielczosci') for rok, rec in sorted(mag.manifest().items())}
    if any(udzialy.values()):
        print('\nRozdzielczosc zrodlowa pikseli (udzial pikseli z danymi; zmienna rozdzielczosc_zrodla):')
        for rok, u in udzialy.items():
            print(f'  {rok}  ' + (', '.join(f'{k} m: {v * 100:.1f}%' for k, v in u.items()) if u else '-'))

    #---POKRYCIE KAZDEGO ROKU DANYMI (czy warstwy sa 'pelne')---
    try:
        nazwa, pokrycie=pokrycie_lat(sciezka)
    except Exception as e:
        print(f'\n(nie udalo sie policzyc pokrycia: {e})')
        return
    print(f'\nPokrycie danymi (udzial obszaru z danymi z dowolnego roku; liczone z poziomu {nazwa}):')
    for rok, u in sorted(pokrycie.items()):
        rec=mag.manifest().get(rok, {})
        uwagi=[]
        if rec.get('pominiete'):
            uwagi.append(f"{len(rec['pominiete'])} arkuszy nie pobralo sie/nie skonwertowalo -> uruchom geo_nmt.py ponownie")
        if rec.get('nieobslugiwane'):
            uwagi.append(f"{len(rec['nieobslugiwane'])} arkuszy bez plikow ASCII/XYZ (inny format, nie konwertowany)")
        if rec.get('status') != 'kompletna':
            uwagi.append('zapis nieukonczony -> uruchom geo_nmt.py ponownie')
        print(f"  {rok}  {u * 100:5.1f}%  " + ('; '.join(uwagi) if uwagi else
              ('(wszystkie arkusze ze skorowidza uzyte)' if u > 0.999 else
               '(brak brakujacych arkuszy - dane tego roku moga po prostu nie obejmowac calego obszaru)')))


def opis_warstw(sciezka, poziom=None):
    '''
    Dla kazdej warstwy czasowej (plastra): zapisany czas, granice roku oraz statystyki wysokosci i roznica wzgledem
    poprzedniej warstwy: ile pikseli zmienilo sie o wiecej niz 0,5 cm (szum, inna rozdzielczosc zrodla), o wiecej
    niz 0,5 m i o wiecej niz 2 m (to sa dopiero realne zmiany terenu: budynki, nasypy, wykopy).
    Po tym widac, czy warstwy to OSOBNE rastry (rozne dane, kazda z wlasnym czasem), a nie jeden raster z nalozonymi
    wszystkimi latami. Domyslnie liczone z NAJDROBNIEJSZEGO poziomu pochodnego (np. 5 m), a gdy poziomow nie ma - z bazy.
    Zwraca (nazwa_store, uklad_czasu, lista slownikow).
    '''
    mag=Magazyn(sciezka)
    grupy=mag.grupy()
    nazwa=poziom or (grupy[1][0] if len(grupy) > 1 else grupy[0][0])
    p=mag.sciezka_grupy(nazwa)
    ds=xr.open_zarr(str(p), consolidated=False)
    uklad={'units': ds['time'].encoding.get('units'), 'calendar': ds['time'].encoding.get('calendar'),
           'granice': 'time_bnds' in ds, 'fragmenty_time': None}
    try:
        uklad['fragmenty_time']=json.loads((p / 'time' / '.zarray').read_text())['chunks'][0]
    except Exception:
        pass
    nmt=ds['nmt']
    wynik=[]
    poprz=None
    for i, t in enumerate(ds['time'].values):
        warstwa=nmt.isel(time=i).load()
        wart=warstwa.values
        ma=~np.isnan(wart)
        rec={'indeks': i, 'rok': int(pd.Timestamp(t).year), 'time': str(t)[:19],
             'granice': ([str(x)[:19] for x in ds['time_bnds'].values[i]] if 'time_bnds' in ds else None),
             'min': float(np.nanmin(wart)) if ma.any() else None, 'srednia': float(np.nanmean(wart)) if ma.any() else None,
             'max': float(np.nanmax(wart)) if ma.any() else None, 'pikseli': int(ma.sum())}
        rec['zmienione']=rec['zm_05m']=rec['zm_2m']=None
        if poprz is not None:
            wspolne=ma & ~np.isnan(poprz)
            if wspolne.any():
                roznica=np.abs(wart[wspolne] - poprz[wspolne])
                rec['zmienione']=float((roznica > 0.005).mean())
                rec['zm_05m']=float((roznica > 0.5).mean())
                rec['zm_2m']=float((roznica > 2.0).mean())
        poprz=wart
        wynik.append(rec)
    return nazwa, uklad, wynik


def znajdz_store_bazowy(cache_dir):
    '''store(y) bazowe w cache_dir (poziomy pochodne _5m itd. maja w *_opis.json rola=poziom i sa pomijane)'''
    wynik=[]
    for p in sorted(Path(cache_dir).glob('*.zarr')):
        migruj_opis(p)
        try:
            if json.loads(plik_opisu(p).read_text(encoding='utf-8')).get('nmtzarr', {}).get('rola') == 'baza':
                wynik.append(p)
        except Exception:
            continue
    return wynik


def main(argv=None, katalog_skryptu=None):
    '''
    python zarr_info.py sciezka/do/store.zarr
    python zarr_info.py            (bez argumentu: szuka store'u w cache_dir z config.json obok skryptu)
    '''
    argv=sys.argv[1:] if argv is None else argv
    if argv:
        sciezka=argv[0]
    else:
        katalog=Path(katalog_skryptu) if katalog_skryptu else Path(__file__).resolve().parent
        konfig=katalog / 'config.json'
        if not konfig.exists():
            print('Uzycie: python zarr_info.py sciezka/do/store.zarr\n'
                  '(bez argumentu skrypt szuka store\'u w cache_dir z config.json - tu go nie ma)')
            return 1
        cache_dir=json.loads(konfig.read_text(encoding='utf-8')).get('cache_dir')
        znalezione=znajdz_store_bazowy(cache_dir) if cache_dir else []
        if not znalezione:
            print(f'Nie znaleziono store\'u (*.zarr z plikiem *_opis.json) w {cache_dir}. Podaj sciezke: '
                  f'python zarr_info.py sciezka/do/store.zarr')
            return 1
        if len(znalezione) > 1:
            print('Znaleziono kilka store\'ow - podaj sciezke jednego z nich:')
            for p in znalezione:
                print(f'  python zarr_info.py "{p}"')
            return 1
        sciezka=str(znalezione[0])
    try:
        info(sciezka)
    except BladMagazynu as e:
        print(f'BLAD: {e}')
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
