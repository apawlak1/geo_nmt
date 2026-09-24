# -*- coding: utf-8 -*-
'''
ZMIANA ZAPISU CZASU W ISTNIEJACYM STORE'ZE (bez ruszania danych rastrowych) - do testow w ArcGIS.

ArcGIS moze inaczej niz GDAL/xarray interpretowac wymiar time. Zamiast przebudowywac store, mozna tym
skryptem w kilka sekund zmienic tylko tablice time (i time_bnds) we wszystkich rozdzielczosciach
(baza + poziomy) i sprawdzic, ktory wariant ArcGIS czyta poprawnie.

    python zarr_czas.py <store_bazowy.zarr> [--pozycja srodek|poczatek] [--granice tak|nie]
                                            [--jednostka sekundy|dni] [--fragmenty-czasu jeden|po_jednym]

  --pozycja    wartosc time warstwy: srodek roku (1 lipca, domyslnie) albo poczatek roku (1 stycznia)
  --granice    czy rok ma przedzial time_bnds: 1 stycznia - 31 grudnia (dla jednostki 'dni': do 1 stycznia
               nastepnego roku, bo w dniach nie da sie zapisac 23:59:59)
  --jednostka  jednostki czasu: 'seconds since 1970-01-01' (domyslnie) albo 'days since 1970-01-01'

  --fragmenty-czasu  jak zapisac tablice time: 'po_jednym' (domyslnie; kazda warstwa dopisana pozniej to osobny plik
               time/0, time/1, ...) albo 'jeden' (cala tablica w jednym pliku time/0 - dla czytnikow, ktore
               wczytuja tylko pierwszy fragment wspolrzednej czasu)

Wariant przywracajacy dotychczasowy domyslny zapis: --pozycja srodek --granice tak --jednostka sekundy.
UWAGA: po wybraniu wariantu bez granic (--granice nie) dopisywanie kolejnych lat do tego store'u jest
zablokowane (kod tworzy nowe warstwy z time_bnds) - to narzedzie do TESTOW; gdy ustalimy dzialajacy wariant,
zmieni sie zapis w kodzie i store zbuduje sie od nowa.
'''

import argparse
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
import zarr

from zarr_magazyn import Magazyn, BladMagazynu

_ZARR3 = int(zarr.__version__.split('.')[0]) >= 3
EPOKA = pd.Timestamp('1970-01-01')


def _kod(ts, jednostka):
    sek = int((pd.Timestamp(ts) - EPOKA).total_seconds())
    return sek if jednostka == 'sekundy' else sek // 86400


def przepisz_czas(sciezka_store, pozycja='srodek', granice='tak', jednostka='sekundy', fragmenty_czasu='po_jednym'):
    '''zwraca {'lata': [...], 'time': [...], 'granice': [...] albo None}'''
    sciezka_store = Path(sciezka_store)
    ds = xr.open_zarr(str(sciezka_store), consolidated=False)
    lata = [int(pd.Timestamp(t).year) for t in ds['time'].values]
    ds.close()

    units = 'seconds since 1970-01-01' if jednostka == 'sekundy' else 'days since 1970-01-01'
    czasy = [pd.Timestamp(r, 7 if pozycja == 'srodek' else 1, 1) for r in lata]
    g = zarr.open_group(str(sciezka_store), mode='r+')

    # ---- time ----
    t = g['time']
    n = len(lata)
    if fragmenty_czasu == 'jeden' or (fragmenty_czasu == 'po_jednym' and t.chunks != (1,) and n > 1):
        atrybuty = dict(t.attrs)
        shutil.rmtree(sciezka_store / 'time')
        fr = n if fragmenty_czasu == 'jeden' else 1
        if _ZARR3:
            t = g.create_array('time', shape=(n,), chunks=(fr,), dtype='int64', overwrite=True)
        else:
            t = g.create_dataset('time', shape=(n,), chunks=(fr,), dtype='i8', overwrite=True)
        for k, v in atrybuty.items():
            t.attrs[k] = v
    t[:] = np.array([_kod(x, jednostka) for x in czasy], dtype='int64')
    t.attrs['units'] = units
    t.attrs['calendar'] = 'standard'
    if granice == 'tak':
        t.attrs['bounds'] = 'time_bnds'
    elif 'bounds' in dict(t.attrs):
        del t.attrs['bounds']

    # ---- time_bnds ----
    if granice == 'tak':
        if jednostka == 'sekundy':
            pary = [(pd.Timestamp(r, 1, 1), pd.Timestamp(r, 12, 31, 23, 59, 59)) for r in lata]
        else:
            pary = [(pd.Timestamp(r, 1, 1), pd.Timestamp(r + 1, 1, 1)) for r in lata]
        wart = np.array([[_kod(a, jednostka), _kod(b, jednostka)] for a, b in pary], dtype='int64')
        if (sciezka_store / 'time_bnds').exists():
            shutil.rmtree(sciezka_store / 'time_bnds')
        fr = n if fragmenty_czasu == 'jeden' else 1
        if _ZARR3:
            b = g.create_array('time_bnds', shape=wart.shape, chunks=(fr, 2), dtype='int64', overwrite=True)
        else:
            b = g.create_dataset('time_bnds', shape=wart.shape, chunks=(fr, 2), dtype='i8', overwrite=True)
        b[:] = wart
        b.attrs['_ARRAY_DIMENSIONS'] = ['time', 'bnds']
        b.attrs['units'] = units
        b.attrs['calendar'] = 'standard'
    elif (sciezka_store / 'time_bnds').exists():
        shutil.rmtree(sciezka_store / 'time_bnds')

    ds = xr.open_zarr(str(sciezka_store), consolidated=False)
    wynik = {'lata': lata, 'time': [str(x)[:19] for x in ds['time'].values],
             'granice': ([[str(a)[:19], str(b)[:19]] for a, b in ds['time_bnds'].values]
                         if 'time_bnds' in ds else None)}
    ds.close()
    return wynik


def przepisz_czas_wszystkich(sciezka_bazy, **kw):
    mag = Magazyn(sciezka_bazy)
    wynik = {}
    for nazwa, _, _ in mag.grupy():
        sciezka = mag.sciezka_grupy(nazwa)
        wynik[nazwa] = (sciezka, przepisz_czas(sciezka, **kw))
    return wynik


def main(argv=None):
    p = argparse.ArgumentParser(description='Zmiana zapisu czasu w store (baza + poziomy) - do testow w ArcGIS')
    p.add_argument('store', help="sciezka do store'u BAZOWEGO (.zarr)")
    p.add_argument('--pozycja', choices=['srodek', 'poczatek'], default='srodek')
    p.add_argument('--granice', choices=['tak', 'nie'], default='tak')
    p.add_argument('--jednostka', choices=['sekundy', 'dni'], default='sekundy')
    p.add_argument('--fragmenty-czasu', choices=['jeden', 'po_jednym'], default='po_jednym')
    a = p.parse_args(argv)
    try:
        wynik = przepisz_czas_wszystkich(a.store, pozycja=a.pozycja, granice=a.granice, jednostka=a.jednostka,
                                          fragmenty_czasu=a.fragmenty_czasu)
    except BladMagazynu as e:
        print(f'BLAD: {e}')
        return 2
    for nazwa, (sciezka, w) in wynik.items():
        print(f'{sciezka.name}: time={w["time"]}')
        if w['granice']:
            print(f'    granice: {w["granice"]}')
    print('\nGotowe. W ArcGIS usun warstwe z mapy i dodaj store od nowa (ArcGIS buforuje odczytany czas).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
