"""
Stock List  =  Google Sheet  +  RSI  +  DMAs  +  Average Volumes

Where each column comes from
  * RSI (14/7) ............ TradingView, ONE request for all stocks, refreshes every 30 s
  * 200 DMA (Weekly) ...... TradingView (needs ~4 years of weekly data), refreshes every 30 s
  * 20 / 50 / 100 / 200 DMA  calculated EXACTLY like the sheet formula:
        =AVERAGE(QUERY(SORT(GOOGLEFINANCE(C6,"Price",TODAY()-365,TODAY()),1,0),"Select Col2 limit 20"))
        -> newest-first daily closes of the last 365 days, take the latest N, average them.
           Completed days only (today's live price is not a finished close yet).
  * Avg Vol (30D/60D/90D) . calculated EXACTLY like the sheet formula:
        =AVERAGE(INDEX(GOOGLEFINANCE(C6,"volume",TODAY()-31,TODAY()-1),,2))
  * Output + "Why AVOID" .. every failed BUY condition is listed, so you can see exactly why a
                            stock is AVOID. An OHLC that is far away from CMP is flagged as a
                            probable sheet error (e.g. a typed-in value instead of the formula).

DMAs and volumes use NSE's official daily bhavcopy files (close + volume of EVERY NSE stock
for one day). Each file is downloaded ONCE and kept in the "nse_data" folder next to app.py,
so the first run takes a minute and after that only 1 new small file is downloaded per day.

Run:  pip install streamlit pandas requests
      streamlit run app.py
"""

import datetime as dt
import io
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

# ====================================================================== SETTINGS
SHEET_ID = "1cUcSnKRCSOQ69OF3bunPhcfdP5iK2EV5vKa7Ad8ueZw"
SHEET_GID = "0"
REFRESH_SEC = 30

# Daily moving averages of closing price: column -> number of closes (from last 365 days)
DMA = {"20 DMA": 20, "50 DMA": 50, "100 DMA": 100, "200 DMA": 200}
DMA_LOOKBACK_DAYS = 365

# Average volume: column -> calendar days back (TODAY()-N ... TODAY()-1)
AVG_VOL = {"Avg Vol (30D)": 31, "Avg Vol (60D)": 61, "Avg Vol (90D)": 91}

# From TradingView (live, every 30 s)
WEEKLY_DMA = {"200 DMA (Weekly)": "SMA200|1W"}
RSI_FIELDS = {"RSI 14": "RSI", "RSI 7": "RSI7"}
TIMEFRAMES = {"Daily": "", "Weekly": "|1W", "Monthly": "|1M", "1 Hour": "|60", "15 Min": "|15"}

RSI_LIMIT = 70
# OHLC more than this far from CMP (as a fraction) is almost certainly a sheet error
OHLC_SANITY = 0.25

RENAMED = {"GUJGASLTD": "GUJENERGY", "HEG": "HEGAM", "MIDWEST": "MIDWESTLTD"}   # old -> new
DELISTED = set()            # symbols to hide; empty = show every row of the sheet

# M Cap for symbols that are in SYMBOLS but not in the Google Sheet (otherwise left blank).
# For those rows CMP + Volume come live from TradingView, and OHLC is calculated as
# (Open + High + Low + Close) / 4 of the previous trading day from NSE's file.
EXTRA_MCAP = {"KMEW": "Microcap"}

# Master list: the app shows exactly these stocks, once each, in this order (989 symbols).
# Add or remove a symbol here (spaces / new lines between symbols, any order).
SYMBOLS = """
RELIANCE HDFCBANK BHARTIARTL SBIN ICICIBANK TCS BAJFINANCE LT INFY HINDUNILVR LICI MARUTI
SUNPHARMA M&M AXISBANK KOTAKBANK ITC TITAN HCLTECH NTPC ADANIPORTS ULTRACEMCO ADANIPOWER ONGC
BEL JSWSTEEL BAJAJFINSV ADANIENT HAL COALINDIA BAJAJ-AUTO POWERGRID DMART NESTLEIND HINDZINC
TATASTEEL ETERNAL ASIANPAINT HINDALCO WIPRO IOC SHRIRAMFIN EICHERMOT GRASIM SBILIFE
ADANIGREEN VAML INDIGO TVSMOTOR DIVISLAB HYUNDAI VBL JIOFIN TMCV ICICIAMC DLF PIDILITIND
BANKBARODA TECHM TRENT TORNTPHARM BPCL ADANIENSOL TATACAP HDFCLIFE LTM MUTHOOTFIN PFC IRFC
CHOLAFIN BRITANNIA CUMMINSIND SOLARINDS UNIONBANK ABB MOTHERSON PNB TMPV CANBK TATAPOWER
POLYCAB IDEA POWERINDIA SIEMENS JINDALSTEL CGPOWER INDIANB AMBUJACEM BAJAJHLDNG INDUSTOWER
GODREJCP TATACONSUM GROWW HDFCAMC APOLLOHOSP BHEL CIPLA GAIL BOSCHLTD ENRIN HEROMOTOCO
DRREDDY LUPIN GMRAIRPORT ASHOKLEY LGEINDIA GVT&D MARICO MAXHEALTH MAZDOCK UNITDSPR ZYDUSLIFE
INDHOTEL IDBI RECLTD LODHA MANKIND SHREECEM JSWENERGY ICICIGI ABCAPITAL WAAREEENER LENSKART
HINDPETRO BHARATFORG ICICIPRULI PERSISTENT DABUR NTPCGREEN HAVELLS SWIGGY SRF LLOYDSME
BHARTIHEXA NHPC AUROPHARMA MEESHO OIL NMDC TORNTPOWER POLICYBZR AUBANK PAYTM BAJAJHFL OFSS
LTF FEDERALBNK NATIONALUM INDUSINDBK SAIL NAUKRI BANKINDIA FORTIS MCX SUZLON DIXON YESBANK
SBICARD GICRE IOB ALKEM ATGL UNOMINDA IDFCFIRSTB SCHAEFFLER PHOENIXLTD JSL NAM-INDIA BIOCON
COROMANDEL PRESTIGE GLENMARK RVNL LINDEINDIA OBEROIRLTY MFSL MRF HDBFS ABBOTINDIA JSWINFRA
COLPAL MAHABANK UPL BERGEPAINT VMM APLAPOLLO FACT SUNDARMFIN TIINDIA HINDCOPPER KRT
GODREJPROP PATANJALI BDL MOTILALOFS COFORGE TATACOMM SUPREMEIND M&MFIN VOLTAS PIIND MPHASIS
IRCTC KEI APARINDS BALKRISIND 360ONE THERMAX PETRONET JKCEMENT PIRAMALFIN ASTRAL RADICO
KALYANKJIL EMBASSY GLAXO PREMIERENE NLCINDIA HUDCO COCHINSHIP ANTHEM PAGEIND UBL IPCALAB
FLUOROCHEM LTTS 3MINDIA AIAENG CONCOR NH AJANTPHARM BLUESTARCO ESCORTS DALBHARAT IREDA
POONAWALLA BAGMANE ENDURANCE ASTERDM ITCHOTELS PGHH UCOBANK SONACOMS GODFRYPHLP GODREJIND
NAVINFLUOR TATAINVEST DELHIVERY HEXT JBCHEPHARM CENTRALBK KPRMILL JUBLFOOD CRISIL ATHERENERG
GLAND MEDANTA PWL CHOLAHLDNG MINDSPACE EMCURE MRPL GODIGIT GRSE LICHSGFIN EXIDEIND SJVN
STARHEALTH TVSHLTD TATAELXSI HONAUT BANDHANBNK GUJGASLTD KARURVYSYA METROBRAND APOLLOTYRE ACC
ZFCVINDIA ABSLAMC KIMS ITI ANANDRATHI MSUMI WELCORP IKS FORCEMOT HSCL NBCC GILLETTE BIRET
PTCIL TATATECH ANGELONE AWL AEGISLOG AMBER IRB NUVAMA TIMKEN NIACL MANAPPURAM CPPLUS
LALPATHLAB KAYNES WOCKPHARMA SHYAMMETL PNBHOUSING NXST SCHNEIDER RAMCOCEM IGL AEGISVOPAK
TENNIND ASAHIINDIA KPITTECH KIOCL KIRLOSENG SUNTV DEEPAKNTR CESC PFIZER SUMICHEM SAILIFE IIFL
PPLPHARMA PINELABS ASTRAZEN HBLENGINE AFFLE NETWEB AADHARHFC PFOCUS CREDITACC KPIL SAGILITY
BAYERCROP EIHOTEL GESHIP RBLBANK ACUTAAS EMAMILTD GMDCLTD DATAPATTNS ATUL ERIS CRAFTSMAN
REDINGTON NEULANDLAB RRKABEL URBANCO SYNGENE SARDAEN GRINDWELL ANANTRAJ TATACHEM GPIL
SUNDRMFAST NSDL CAMS CHAMBLFERT SYRMA PSB BRIGADE ONESOURCE FSL BELRISE CGCL JYOTICNC CHALET
TRITURBINE NATCOPHARM DCMSHRIRAM CARBORUNIV CIEINDIA EMMVEE RATNAMANI NAVA JSWCEMENT
KANSAINER CENTURYPLY BHARATCOAL IFCI CROMPTON INOXWIND ACMESOLAR ELGIEQUIP KAJARIACER BIKAJI
JSWHL VEDPOWER KFINTECH ECLERX GRANULES TRAVELFOOD AARTIIND GSPL AVANTIFEED LMW VTL VENTIVE
CHOICEIN GALLANTT BASF FRACTAL PGEL KEC ARE&M CMPDI TDPOWERSYS OLAELEC DEEPAKFERT JUBLPHARMA
MTARTECH SOBHA POLYMED EIDPARRY ABREL RUBICON APLLTD ANURAS CHENNPETRO MAHSCOOTER ZYDUSWELL
IGIL SAMMAANCAP VINATIORGA DEVYANI SANSERA NIVABUPA AGARWALEYE AETHER BEML JBMA GABRIEL
THELEELA CEATLTD SHRIPISTON CUPID TBOTEK JAINREC CAPLIPOINT CCL DOMS KSB FINCABLES VGUARD
STLTECH J&KBANK IRCON LTFOODS JSWDULUX CANHLIFE ZENTEC USHAMART MINDACORP RRP FIVESTAR
JINDALSAW CEMPRO VOGL GRAPHITE KIRLOSBROS TRIDENT PARADEEP TEGA TECHNOE ZENSARTECH RAINBOW
ABLBL UTIAMC SPLPETRO APTUS WELSPUNLIV INDIAMART JKTYRE CLEANMAX INDIACEM FIRSTCRY BLUEDART
SCI JMFINANCIL THANGAMAYL CONCORDBIO NSLNISP INGERRAND INOXINDIA SIGNATURE JWL ALIVUS
ENGINERSIN HAPPYFORGE PRIVISCL GRAVITA AFCONS SKFINDUS CANFINHOME AZAD JPPOWER NUVOCO
HOMEFIRST BBOX IEX SWANCORP BLS PCBL RPOWER BBTC UJJIVANSFB VIJAYA GODREJAGRO LUMAXTECH
FINPIPE HEG WHIRLPOOL MGL HONASA EDELWEISS SHAILY SOUTHBANK PRUDENT VISL AAVAS CYIENT ACE
MANYAVAR BLACKBUCK ASTRAMICRO TMB INTELLECT TITAGARH SANDUMA ARVIND BSOFT JUBLINGREA SBFC
SANOFICONR ELECON GRWRHITECH RITES MEDPLUS RAILTEL METROPOLIS TI APOLLO NAZARA SWANDEF
ATLANTAELE RKFORGE CORONA BALRAMCHIN VESUVIUS BATAINDIA WAAREERTL MMTC CELLO EUREKAFORB NCC
OLECTRA LEMONTREE PARKHOSPS VOLTAMP STAR SHARDACROP ESABINDIA GRINFRA PGHL TARIL TIMETECHNO
SEDEMAC BANCOINDIA STARCEMENT MARKSANS HCG KTKBANK ASKAUTOLTD LLOYDSENT VIYASH KIRLPNU ZEEL
WABAG AMAGI JYOTHYLAB JLHL JAYNECOIND VARROC RHIM KAMAHOLD INDIASHLTR TTML SAFARI TRIVENI
SKFINDIA SANOFI MANORAMA RELAXO CLEAN JKLAKSHMI ORKLAINDIA KPIGREEN ELITECON DIACABS EMBDL
NESCO GMRP&UI JSLL IXIGO ALKYLAMINE KRBL AKUMS ABFRL PNGJL MAHSEAMLES GPPL SONATSOFTW
TEJASNET SHILPAMED RELIGARE AVALON V2RETAIL EQUITASBNK CAMPUS BIRLACORPN QPOWER BLUEJET TCI
WESTLIFE VIKRAMSOLR UTLSOLAR WEWORK TIPSMUSIC MAHLIFE SUDEEPPHRM NEWGEN RATEGAIN BLUESTONE
TTKPRESTIG IMFA POWERMECH DBL PCJEWELLER BENGALASM RCF SAREGAMA KIRLFER E2E PRICOLLTD
TRANSRAILL YATHARTH THYROCARE ACI LATENTVIEW SHAKTIPUMP GNFC SUDARSCHEM BORORENEW REDTAPE
MIDHANI ALOKINDS EPL WELENT CSBBANK CERA DYNAMATECH ISGEC INOXGREEN GAEL DODLA GSFC KALPATARU
KWIL AVL MAXESTATES GALAXYSURF ETHOSLTD SHREEJISPG GARFIBRES TANLA POWERICA AARTIPHARM
JAIBALAJI PRSMJOHNSN MOIL INDIAGLYCO PARAS SFL SKYGOLD PRAJIND BECTORFOOD PRECWIRE ASHAPURMIN
KRN FDC SAPPHIRE ARVINDFASN HNDFDS GREENLAM SGMART DBREALTY SUPRAJIT AJAXENGG KOVAI JKPAPER
DCBBANK SMLMAH HAPPSTMNDS SUPRIYA FIEMIND SJS MAPMYINDIA GANESHHOU GOKULAGRO NEPHROPLUS
MASFIN PICCADIL ICIL BAJAJCON LGBBROSLTD AHLUCONT FEDFINA SYMPHONY PNCINFRA IONEXCHANG
CMRGREEN CEIGALL BALUFORGE KENNAMET RUSTOMJEE SAATVIKGL ICRA MASTEK SENCO NETWORK18 RENUKA
TIIL MHRIL PTC HCC CCAVENUE SURYAROSNI MIDWEST RAYMONDLSL WAKEFIT PURVA SUNTECK CRAMC
PILANIINVS THOMASCOOK ENTERO CMSINFO SPARC JAMNAAUTO GULFOILLUB LUMAXIND GOKEX SMARTWORKS
TVSSCS SUBROS VMART RALLIS JUSTDIAL SHARDAMOTR SIS NIRLON AURIONPRO CARERATING RTNPOWER
SKIPPER MIDWESTLTD DHANUKA VIPIND JUNIPER IFBIND ELECTCAST OMNI RTNINDIA SUVEN EPIGRAL ITDC
SHILCTECH SWSOLAR RESPONIND INDIGOPNTS VSTTILLERS BANARISUG KSCL VRLLOG SWARAJENG BANSALWIRE
GHCL BALAMINES TEXRAIL MANINFRA DATAMATICS PDSL JSFB INNOVACAP OSWALPUMPS CAPILLARY BAJAJELEC
KISSHT ASMTEC NIITMTS TANFACIND GUJALKALI NORTHARC SENORES GOLDIAM CENTUM STYL STYLAMIND TARC
VSTIND GMMPFAUDLR AVANTEL WPIL TURTLEMINT AGIIL EMIL HAWKINCOOK NEOGEN LXCHEM DHENUBUILD
DBCORP JKIL ARTEMISMED RELINFRA AGI CRIZAC CHEMPLASTS HEMIPROP GREAVESCOT HGINFRA POCL
EMUDHRA ANUP PAISALO PACEDIGITK RBA SIGMAADV AEROFLEX RAJESHEXPO NFL ASHOKA MOSCHIP
ORIENTELEC CIANAGRO JTEKTINDIA VAIBHAVGBL GUJTHEM SHOPERSTOP KNRCON GVPIL TRUALT TECHNVISN
BOSCH-HCIL VISHNU WEBELSOLAR FOSECOIND NACLIND ROLEXRINGS STYRENIX INDRAMEDCO MAHLOG GOPAL
CIGNITITEC KSHINTL SINDHUTRAD SAMHI BONDADA ELLEN HEIDELBERG IBULLSLTD INDIQUBE ADVENZYMES
SEAMECLTD SANATHAN RSYSTEMS UEL HARSHA ORCHPHARMA SMSPHARMA VADILALIND REFEX LUXIND
SHANTIGEAR INDOSTAR ARSSBL TIMEX COLAB AARTIDRUGS MANINDS KESAR ROUTE KITEX MSTCLTD INDOTHAI
ZAGGLE ASHIANA SANDHAR RAMRAT KSL PITTIENG KIRLOSIND TFCILTD RAYMONDREL RAMKY HERITGFOOD
FLAIR AYE INTERARCH EIEL WONDERLA KOLTEPATIL NAVNETEDUL FCL UFLEX GRAUWEIL GUFICBIO SBCL
PSPPROJECT QUESS MARATHON BAJAJHIND ROSSTECH MUTHOOTMF SHAREINDIA SAMBHV KDDL BALMLAWRIE
ORIENTCEM CARRARO GREENPLY TVSSRICHAK CYIENTDLM WSTCSTPAPR BBL HUBTOWN DCAL BHAGCHEM EFCIL
RAYMOND NSIL RPTECH 63MOONS NRBBEARING MPSLTD SUNCLAY SEIL BOROLTD PREMEXPLN MAITHANALL
UNIVCABLES KKCL GATEWAY HIRECT COCKERILL NYKAA LAURUSLABS AIIL HATSUN CUB CASTROLIND HFCL
COHANCE ABDL FINEORG INDGN AEQUS IIFLCAPS CARTRADE PVRINOX SHADOWFAX LLOYDSENGG PGIL ELANTAS
AXISCADES LOTUSDEV KINGFA SUNFLAG RAIN UNIMECH KMEW EBGNG GOODLUCK RPEL OPTIEMUS RPGLIFE SSWL
TATVA
""".split()

DATA_DIR = Path(__file__).parent / "nse_data"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TV_URL = "https://scanner.tradingview.com/india/scan"
TV_HEADERS = {"content-type": "application/json", "origin": "https://www.tradingview.com",
              "referer": "https://www.tradingview.com/", "user-agent": UA}
BHAV_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{:%Y%m%d}_F_0000.csv.zip"
EQ_SERIES = {"EQ", "BE", "BZ", "SM", "ST"}

st.set_page_config(page_title="Stock List", page_icon="📋", layout="wide")


# ====================================================================== HELPERS
def match_key(symbol) -> str:
    """'M&M' -> 'MM' (letters and digits only)."""
    return re.sub(r"[^A-Z0-9]", "", str(symbol).upper())


def today_ist() -> dt.date:
    return pd.Timestamp.now(tz="Asia/Kolkata").date()


def num(s: pd.Series) -> pd.Series:
    """'₹1,213.65' -> 1213.65"""
    return pd.to_numeric(s.astype(str).str.replace(r"[^0-9.\-]", "", regex=True), errors="coerce")


# ====================================================================== GOOGLE SHEET
@st.cache_data(ttl=600, show_spinner="Loading Google Sheet…")
def load_sheet() -> pd.DataFrame:
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
    df = pd.read_csv(url, dtype=str, keep_default_na=False)
    df.columns = [" ".join(str(c).split()) for c in df.columns]
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
    df = df.apply(lambda c: c.str.strip())
    df = df.replace({"": pd.NA, "#N/A": pd.NA, "N/A": pd.NA})
    df = df.dropna(how="all").dropna(axis=1, how="all")
    df = df.drop(columns=["Close"], errors="ignore")

    # ---- one row per symbol, in SYMBOLS order
    tick = df["Ticker"].fillna("").str.upper() if "Ticker" in df.columns else pd.Series("", index=df.index)
    sym = df["Symbol"].fillna("").str.upper() if "Symbol" in df.columns else tick.str.split(":").str[-1]
    df["_s"] = sym.where(sym != "", tick.str.split(":").str[-1])
    # When a symbol is in the sheet twice, keep the row whose Ticker has "NSE:" (without it
    # GOOGLEFINANCE can pick a foreign stock), then the row that has a CMP number.
    df["_rank"] = (~tick.str.startswith("NSE:")).astype(int) * 2 + num(df.get("CMP", tick)).isna().astype(int)
    repeats = int(df["_s"].duplicated().sum())
    df = df.sort_values("_rank", kind="stable").drop_duplicates("_s").set_index("_s")

    master = list(dict.fromkeys(s.upper() for s in SYMBOLS))
    missing = [s for s in master if s not in df.index]
    not_listed = [s for s in df.index if s and s not in master]
    df["_extra"] = False
    if missing:                      # in SYMBOLS but not in the sheet: CMP/Volume/OHLC filled later
        add = pd.DataFrame({"M Cap": [EXTRA_MCAP.get(s, pd.NA) for s in missing], "Symbol": missing,
                            "Ticker": [f"NSE:{s}" for s in missing], "_extra": True}, index=missing)
        df = pd.concat([df, add])
    df = df.loc[master].reset_index(drop=True).drop(columns="_rank")
    df.attrs.update(repeats=repeats, missing=missing, not_listed=not_listed)

    src = df["Ticker"] if "Ticker" in df.columns else df["Symbol"]
    if "Symbol" in df.columns:
        src = src.fillna(df["Symbol"])
    src = src.fillna("").astype(str).str.upper().str.strip()
    df["_sym"] = src.map(lambda t: t.split(":", 1)[1] if ":" in t else t).replace(RENAMED)
    df["_key"] = df["_sym"].map(match_key)
    info = dict(df.attrs)
    df = df[~df["_sym"].isin(DELISTED)].reset_index(drop=True)
    df.attrs.update(info)
    return df


# ====================================================================== TRADINGVIEW (RSI + weekly DMA)
def fetch_tv(columns: dict) -> pd.DataFrame:
    """Requested TradingView fields for every Indian stock in ONE request.
    columns = {"Column name": "tv_field"}. Indexed by key; NSE wins over BSE."""
    try:
        sid = st.secrets.get("tv_sessionid", "")
    except Exception:
        sid = ""
    payload = {"markets": ["india"], "columns": ["name", *columns.values()],
               "range": [0, 20000], "options": {"lang": "en"}}
    r = requests.post(TV_URL, json=payload, headers=TV_HEADERS,
                      cookies={"sessionid": sid} if sid else {}, timeout=20)
    r.raise_for_status()
    rows = []
    for row in r.json().get("data") or []:
        name, *values = row["d"]
        rows.append([match_key(name or row["s"].split(":")[-1]),
                     0 if row["s"].startswith("NSE:") else 1, *values])
    df = pd.DataFrame(rows, columns=["_key", "_bse", *columns.keys()])
    df = df.sort_values("_bse").drop_duplicates("_key").set_index("_key").drop(columns="_bse")
    return df.apply(pd.to_numeric, errors="coerce").round(2)


# ====================================================================== NSE BHAVCOPY (close + volume)
def _parse_bhav(raw: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        df = pd.read_csv(z.open(z.namelist()[0]))
    df.columns = df.columns.str.strip()
    pick = lambda *names: next(c for c in names if c in df.columns)
    sym, ser = pick("TckrSymb", "SYMBOL"), pick("SctySrs", "SERIES")
    close, vol = pick("ClsPric", "CLOSE"), pick("TtlTradgVol", "TOTTRDQTY")
    df = df[df[ser].astype(str).str.strip().isin(EQ_SERIES)]
    out = pd.DataFrame({"key": df[sym].astype(str).str.strip().map(match_key),
                        "close": pd.to_numeric(df[close], errors="coerce"),
                        "volume": pd.to_numeric(df[vol], errors="coerce")})
    return out.sort_values("volume", ascending=False).drop_duplicates("key")


def _get_day(session: requests.Session, day: dt.date) -> str:
    """Make sure one day's file is in the local folder. Returns 'ok', 'holiday' or 'failed'."""
    csv_file, none_file = DATA_DIR / f"{day:%Y%m%d}.csv", DATA_DIR / f"{day:%Y%m%d}.none"
    if csv_file.exists():
        return "ok"
    if none_file.exists():
        return "holiday"
    for attempt in range(4):                              # NSE often refuses fast requests: retry
        try:
            r = session.get(BHAV_URL.format(day), timeout=20)
            if r.status_code == 200 and r.content.startswith(b"PK"):
                _parse_bhav(r.content).to_csv(csv_file, index=False)
                return "ok"
            if r.status_code == 404:
                none_file.touch()                         # no trading that day: never ask again
                return "holiday"
        except Exception:
            pass
        time.sleep(1 + attempt * 2)
    return "failed"


def sync_history(day: dt.date) -> list:
    """Download any missing NSE files for the last year. Returns the days that FAILED."""
    DATA_DIR.mkdir(exist_ok=True)
    longest = max(DMA_LOOKBACK_DAYS, *AVG_VOL.values())
    days = [day - dt.timedelta(days=i) for i in range(1, longest + 1)]
    days = [d for d in days if d.weekday() < 5]                         # NSE is closed on weekends
    missing = [d for d in days if not (DATA_DIR / f"{d:%Y%m%d}.csv").exists()
               and not (DATA_DIR / f"{d:%Y%m%d}.none").exists()]
    if not missing:
        return []
    session = requests.Session()
    session.headers.update({"user-agent": UA, "referer": "https://www.nseindia.com/"})
    with st.spinner(f"Downloading {len(missing)} NSE daily files (only missing ones)…"):
        with ThreadPoolExecutor(4) as pool:
            status = list(pool.map(lambda d: _get_day(session, d), missing))
    return [d for d, s in zip(missing, status) if s == "failed"]


@st.cache_data(ttl=3600, show_spinner=False)
def prev_day_ohlc(day: dt.date) -> pd.Series:
    """(Open+High+Low+Close)/4 of the last trading day before `day`, for every NSE stock.
    Used only for symbols that are not in the sheet (sheet rows bring their own OHLC). Saved in nse_data/ohlc/."""
    folder = DATA_DIR / "ohlc"
    folder.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"user-agent": UA, "referer": "https://www.nseindia.com/"})
    for i in range(1, 12):
        d = day - dt.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        f = folder / f"{d:%Y%m%d}.csv"
        if not f.exists():
            r = session.get(BHAV_URL.format(d), timeout=20)
            if r.status_code == 404:                     # holiday
                continue
            if r.status_code != 200 or not r.content.startswith(b"PK"):
                raise RuntimeError(f"NSE refused the file for {d:%d %b}")
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                raw = pd.read_csv(z.open(z.namelist()[0]))
            raw.columns = raw.columns.str.strip()
            pick = lambda *names: next(c for c in names if c in raw.columns)
            raw = raw[raw[pick("SctySrs", "SERIES")].astype(str).str.strip().isin(EQ_SERIES)]
            cols = [pick("OpnPric", "OPEN"), pick("HghPric", "HIGH"),
                    pick("LwPric", "LOW"), pick("ClsPric", "CLOSE")]
            out = pd.DataFrame({"key": raw[pick("TckrSymb", "SYMBOL")].astype(str).str.strip().map(match_key),
                                "ohlc": raw[cols].apply(pd.to_numeric, errors="coerce").mean(axis=1)})
            out.drop_duplicates("key").to_csv(f, index=False)
        return pd.read_csv(f).set_index("key")["ohlc"].round(2)
    raise RuntimeError("No NSE trading day found in the last 11 days")


@st.cache_data(show_spinner="Calculating DMA and average volume…")
def nse_stats(day: dt.date, files: int) -> pd.DataFrame:
    """Per stock: 20/50/100/200 DMA and 30/60/90-day average volume from the saved files.
    `files` is in the cache key, so new downloads are picked up automatically."""
    frames = {}
    for f in DATA_DIR.glob("*.csv"):
        d = dt.datetime.strptime(f.stem, "%Y%m%d").date()
        if day - dt.timedelta(days=max(DMA_LOOKBACK_DAYS, *AVG_VOL.values())) <= d < day:
            frames[d] = pd.read_csv(f).set_index("key")
    if not frames:
        raise RuntimeError("No NSE data downloaded yet")

    newest_first = sorted(frames, reverse=True)
    closes = pd.DataFrame({d: frames[d]["close"] for d in newest_first})   # rows=stocks, cols=days
    volumes = pd.DataFrame({d: frames[d]["volume"] for d in newest_first})
    stats = pd.DataFrame(index=closes.index)

    # DMA = AVERAGE(QUERY(SORT(GOOGLEFINANCE(.., "Price", TODAY()-365, TODAY()), 1, 0), "Select Col2 limit N"))
    in_year = [d for d in newest_first if d >= day - dt.timedelta(days=DMA_LOOKBACK_DAYS)]
    long = closes[in_year].stack()                         # (stock, day) newest first, gaps dropped
    rank = long.groupby(level=0).cumcount()
    for col, n in DMA.items():
        stats[col] = long[rank < n].groupby(level=0).mean().round(2)

    # Avg Vol = AVERAGE(INDEX(GOOGLEFINANCE(.., "volume", TODAY()-N, TODAY()-1), , 2))
    for col, n in AVG_VOL.items():
        window = [d for d in newest_first if d >= day - dt.timedelta(days=n)]
        stats[col] = volumes[window].where(volumes[window] > 0).mean(axis=1).round()
    return stats


# ====================================================================== PAGE
try:
    data = load_sheet()
except Exception as e:
    st.error(f"Couldn't load the Google Sheet: {e}")
    st.info("Share the sheet as **Anyone with the link → Viewer**.")
    st.stop()

_info = data.attrs
if _info.get("not_listed"):
    st.info(f"In the Google Sheet but not in SYMBOLS, so not shown: {', '.join(_info['not_listed'])}")

STAT_COLS = [*DMA, *AVG_VOL]
today = today_ist()
failed = sync_history(today)
if len(failed) > 5:
    # Never show numbers built from an incomplete history
    for col in STAT_COLS:
        data[col] = None
    st.error(f"NSE refused {len(failed)} daily file(s) (e.g. {failed[0]:%d %b %Y}), so DMA and "
             "Avg Vol are hidden instead of showing wrong numbers. Press **🔄 Reload** in a minute to retry.")
else:
    if failed:
        st.warning("These days couldn't be downloaded from NSE (holiday or refused): "
                   + ", ".join(f"{d:%d %b}" for d in failed) + ". Press **🔄 Reload** later to retry.")
    try:
        stats = nse_stats(today, len(list(DATA_DIR.glob("*"))))
        for col in STAT_COLS:
            data[col] = data["_key"].map(stats[col])
    except Exception as e:
        for col in STAT_COLS:
            data[col] = None
        st.warning(f"Couldn't calculate DMA / average volume: {e}")

if data["_extra"].any():
    try:
        ohlc = prev_day_ohlc(today)
        extra_rows = data["_extra"]
        data.loc[extra_rows, "OHLC"] = data.loc[extra_rows, "_key"].map(ohlc).map(
            lambda v: f"₹{v:,.2f}" if pd.notna(v) else pd.NA)
    except Exception as e:
        st.warning(f"Couldn't get OHLC for the extra stocks ({', '.join(data.loc[data['_extra'], '_sym'])}): {e}")

# ====================================================================== SIDEBAR
st.sidebar.header("📈 RSI")
rsi_label = st.sidebar.radio("Period", list(RSI_FIELDS), horizontal=True)
tf_label = st.sidebar.selectbox("Timeframe", list(TIMEFRAMES))
field = RSI_FIELDS[rsi_label] + TIMEFRAMES[tf_label]
rsi_col = f"{rsi_label} ({tf_label})"

st.sidebar.header("🔎 Filters")
view = data
if "M Cap" in data.columns:
    caps = st.sidebar.multiselect("M Cap", sorted(data["M Cap"].dropna().unique()), placeholder="All")
    if caps:
        view = view[view["M Cap"].isin(caps)]
search = st.sidebar.text_input("Search symbol", placeholder="e.g. RELIANCE")
if search:
    view = view[view["_sym"].str.contains(search, case=False, na=False)]
only_buy = st.sidebar.checkbox("Show only BUY")
if st.sidebar.button("🔄 Reload"):
    st.cache_data.clear()
    st.rerun()

st.title("📋 Stock List")


# ====================================================================== SIGNAL
def add_signal(t: pd.DataFrame) -> pd.DataFrame:
    """Sheet formula:  =IF(AND(E>K, K>L, L>M, M>N, G<70, E>D), "BUY", "AVOID")
       D = OHLC   E = CMP   G = RSI   K = 20 DMA   L = 50 DMA   M = 100 DMA   N = 200 DMA

    Adds two columns:
      Output     BUY / AVOID (same rule as the sheet)
      Why AVOID  every condition that failed, e.g. "CMP 771.85 ≤ OHLC 1195.80 (OHLC looks wrong)"
    """
    D, E, G = num(t["OHLC"]), num(t["CMP"]), t[rsi_col]
    K, L, M, N = t["20 DMA"], t["50 DMA"], t["100 DMA"], t["200 DMA"]

    values = {"OHLC": D, "CMP": E, "RSI": G, "20 DMA": K, "50 DMA": L, "100 DMA": M, "200 DMA": N}
    # (left name, right name, passes?) — the BUY conditions in sheet order
    checks = [("CMP", "20 DMA", E > K), ("20 DMA", "50 DMA", K > L), ("50 DMA", "100 DMA", L > M),
              ("100 DMA", "200 DMA", M > N), ("RSI", None, G < RSI_LIMIT), ("CMP", "OHLC", E > D)]

    # OHLC that is miles away from CMP is a sheet error (typed value / broken formula)
    ohlc_bad = ((D - E).abs() / E) > OHLC_SANITY

    reasons = []
    for i in t.index:
        why = [f"{name} missing" for name, s in values.items() if pd.isna(s[i])]
        if not why:                                   # all numbers present: list failed checks
            for left, right, ok in checks:
                if ok[i]:
                    continue
                if right is None:
                    why.append(f"RSI {G[i]:.2f} ≥ {RSI_LIMIT}")
                else:
                    why.append(f"{left} {values[left][i]:.2f} ≤ {right} {values[right][i]:.2f}")
            if ohlc_bad[i] and not (E[i] > D[i]):
                why[-1] += " (OHLC looks wrong — check the sheet)"
        reasons.append(", ".join(why))

    t["Why AVOID"] = reasons
    t["Output"] = ["BUY" if not r else "AVOID" for r in reasons]
    t["_ohlc_bad"] = ohlc_bad.fillna(False)
    return t


def style(t: pd.DataFrame):
    """Whole row red for BUY; OHLC cell yellow when it looks wrong."""
    red = "background-color: #ffcdd2; color: #b71c1c; font-weight: 600"
    yellow = "background-color: #fff59d; color: #6d4c00; font-weight: 600"
    fmt = {**{c: "{:.2f}" for c in [rsi_col, *DMA, *WEEKLY_DMA]},
           **{c: "{:,.0f}" for c in AVG_VOL}}
    t = t.copy()
    bad = t.pop("_ohlc_bad")

    def row_style(row):
        css = [red if row["Output"] == "BUY" else ""] * len(row)
        if bad[row.name] and "OHLC" in row.index:
            css[row.index.get_loc("OHLC")] = yellow
        return css

    return (t.style
            .apply(row_style, axis=1)
            .format({k: v for k, v in fmt.items() if k in t.columns}, na_rep=""))


# ====================================================================== LIVE TABLE
@st.fragment(run_every=REFRESH_SEC)
def live_table():
    now = pd.Timestamp.now(tz="Asia/Kolkata")
    table = view.copy()
    tv_cols = {rsi_col: field, **WEEKLY_DMA, "_tv_close": "close", "_tv_volume": "volume"}
    try:
        tv = fetch_tv(tv_cols)
        for col in tv_cols:
            table[col] = table["_key"].map(tv[col])
    except Exception as e:
        for col in tv_cols:
            table[col] = float("nan")
        st.warning(f"Couldn't get RSI from TradingView: {e}")

    # Extra stocks (not in the sheet): live CMP and Volume from TradingView
    ex = table["_extra"]
    if ex.any():
        table.loc[ex, "CMP"] = table.loc[ex, "_tv_close"].map(lambda v: f"₹{v:,.2f}" if pd.notna(v) else pd.NA)
        if "Volume" in table.columns:
            table.loc[ex, "Volume"] = table.loc[ex, "_tv_volume"].map(lambda v: f"{v:.0f}" if pd.notna(v) else pd.NA)
    for col in STAT_COLS:
        table[col] = pd.to_numeric(table[col], errors="coerce")

    table = add_signal(table)

    bad_syms = table.loc[table["_ohlc_bad"], "_sym"].tolist()
    if bad_syms:
        st.warning(f"OHLC in the sheet looks wrong (more than {OHLC_SANITY:.0%} away from CMP) for: "
                   + ", ".join(bad_syms) + ". These show AVOID only because of that cell — "
                   "copy the OHLC formula from a working row into them.")

    m = st.columns(3)
    m[0].metric("Total stocks", len(table))
    m[1].metric("🔴 BUY", int((table["Output"] == "BUY").sum()))
    m[2].metric("⚠️ Bad OHLC", len(bad_syms))
    st.caption(f"Updated {now:%I:%M:%S %p} IST · RSI refreshes every {REFRESH_SEC} s · "
               f"BUY = CMP > 20 DMA > 50 DMA > 100 DMA > 200 DMA, RSI < {RSI_LIMIT}, CMP > OHLC")

    if only_buy:
        table = table[table["Output"] == "BUY"]

    # put Output and Why AVOID at the end, next to each other
    cols = [c for c in table.columns
            if c not in ("Output", "Why AVOID") and (not c.startswith("_") or c == "_ohlc_bad")]
    table = table[[*cols, "Output", "Why AVOID"]]

    st.dataframe(style(table), hide_index=True, width="stretch", height=650,
                 column_config={"Why AVOID": st.column_config.TextColumn(width="large")})
    st.download_button("⬇️ Download CSV",
                       table.drop(columns="_ohlc_bad").to_csv(index=False).encode("utf-8-sig"),
                       "stock_list.csv", "text/csv")


live_table()