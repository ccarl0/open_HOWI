# Airfoil coordinates

The `.dat` files in this folder are a selection from the **UIUC Airfoil Coordinates Database**,
compiled by Michael Selig and the UIUC Applied Aerodynamics Group
(<https://m-selig.ae.illinois.edu/ads/coord_database.html>). The airfoils themselves were designed
by their respective authors (Eppler, Selig, Drela, Hepperle, Wortmann, Girsberger, Quabeck, NACA
and others), as named in each file.

The files were taken unchanged from the copy of that database distributed with
[AeroSandbox](https://github.com/peterdsharpe/AeroSandbox) 4.2.10, under this license:

```
MIT License

Copyright (c) 2019-2023 Peter Sharpe

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Categories and notes are kept in `openhowi/airfoildb.py`. NACA 4-digit sections are generated,
not stored. To add an airfoil, put its file here and add an `Entry` for it.
