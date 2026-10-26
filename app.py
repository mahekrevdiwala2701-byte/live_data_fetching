"""
Stock List  =  Google Sheet  +  RSI  +  DMAs  +  Average Volumes

Where each column comes from
  * RSI (14/7) ............ TradingView, ONE request for all stocks
  * 20/50/100/200 DMA ..... the sheet's values; blanks are filled with the average of the last
                            N daily closes from NSE files (split / bonus adjusted)
  * 200 EMA (weekly) ...... the sheet's value; blanks filled from TradingView (EMA200, weekly chart)
  * OHLC .................. from the Google Sheet; when the sheet shows "Data not available"
                            (or any non-number) it is calculated as (Open+High+Low+Close)/4
                            of the previous trading day from NSE's official file
  * The whole page reloads every 5 minutes (REFRESH_SEC)
  * 20 / 50 / 100 / 200 DMA  calculated EXACTLY like the sheet formula:
        =AVERAGE(QUERY(SORT(GOOGLEFINANCE(C6,"Price",TODAY()-365,TODAY()),1,0),"Select Col2 limit 20"))
        -> newest-first daily closes of the last 365 days, take the latest N, average them.
           Completed days only (today's live price is not a finished close yet).
  * Avg Vol (30D/60D/90D) . calculated EXACTLY like the sheet formula:
        =AVERAGE(INDEX(GOOGLEFINANCE(C6,"volume",TODAY()-31,TODAY()-1),,2))
  * Output (BUY / AVOID) .. BUY when a stock passes every condition switched on in the sidebar
                            (RSI range, DMA order, CMP > OHLC, volume, CMP % range). Press Apply
                            after changing them. Default = the sheet formula.

DMAs and volumes use NSE's official daily bhavcopy files (close + volume of EVERY NSE stock
for one day). Each file is downloaded ONCE and kept in the "nse_data" folder next to app.py,
so the first run takes a minute and after that only 1 new small file is downloaded per day.

Run:  pip install streamlit pandas requests
      streamlit run app.py
"""

import datetime as dt
import html
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
REFRESH_SEC = 300            # whole page (Google Sheet + TradingView + signals) reloads every 5 min

# Daily moving averages of closing price: column -> number of closes (from last 365 days)
DMA = {"20 DMA": 20, "50 DMA": 50, "100 DMA": 100, "200 DMA": 200}
DMA_LOOKBACK_DAYS = 365

# Average volume: column -> calendar days back (TODAY()-N ... TODAY()-1)
AVG_VOL = {"Avg Vol (30D)": 31, "Avg Vol (60D)": 61, "Avg Vol (90D)": 91}

# From TradingView (live, every page reload)
ADJ_TOLERANCE = 0.002          # prev-close jump bigger than 0.2 % on an ex-date = split / bonus / rights
# 200 EMA (weekly): the Google Sheet's value; TradingView's EMA200 on the weekly chart only fills blanks
# (an EMA needs the stock's FULL weekly history to be exact, which TradingView has)
WEEKLY_DMA = {"200 EMA (Weekly)": "EMA200|1W"}
SHOW_MA = [*DMA, *WEEKLY_DMA]  # the only moving-average columns shown: 20/50/100/200 DMA + 200 EMA (weekly)
RSI_FIELDS = {"RSI 14": "RSI", "RSI 7": "RSI7"}           # daily RSI from TradingView

# ---- Screener: every number a condition can use (Field / Compare to)
FIELDS = ["CMP", "CMP %", "OHLC", "RSI 14", "RSI 7", "20 DMA", "50 DMA", "100 DMA", "200 DMA",
          *WEEKLY_DMA, "Volume", "Avg Vol (30D)", "Avg Vol (60D)", "Avg Vol (90D)"]
# Arithmetic on the "than" field:  e.g.  CMP ≥ 200 EMA (Weekly) × 1.01
MATH = {"": "—", "×": "×", "÷": "÷", "+": "+", "−": "−", "+%": "+ %", "−%": "− %"}
OPS = [">", ">=", "<", "<=", "="]
NUMBER = "Number"                     # "Compare to" = a fixed number (typed in the Number column)


def cond(field, op, other, number=None, math="", on=True):
    """One condition:  Field  Op  (Compare to  Math  Number)   — or  Field  Op  Number."""
    return {"On": on, "Field": field, "Op": op, "Compare to": other, "Math": math, "Number": number}


# Ready-made queries (pick one in the sidebar, then edit / add / delete rows freely)
PRESETS = {
    # YOUR MAIN QUERY (sheet "Output" column):
    # =IF(AND(CMP>20 DMA, 20 DMA>50 DMA, 50 DMA>100 DMA, 100 DMA>200 DMA, RSI<70, CMP>OHLC),"BUY","AVOID")
    "My main query (default)": [
        cond("CMP", ">", "20 DMA"), cond("20 DMA", ">", "50 DMA"), cond("50 DMA", ">", "100 DMA"),
        cond("100 DMA", ">", "200 DMA"), cond("RSI 14", "<", NUMBER, 70), cond("CMP", ">", "OHLC")],
    # sheet "Output 2" column:  =IF(AND(E>=R*1.01, E<=R*1.03),"Buy","Avoid")   (R = 200 EMA weekly)
    "Sheet Output 2 (CMP 1–3% above 200 EMA weekly)": [
        cond("CMP", ">=", "200 EMA (Weekly)", 1.01, "×"), cond("CMP", "<=", "200 EMA (Weekly)", 1.03, "×")],
    "Momentum, not overbought": [
        cond("CMP", ">", "20 DMA"), cond("20 DMA", ">", "50 DMA"), cond("RSI 14", ">=", NUMBER, 55),
        cond("RSI 14", "<", NUMBER, 70), cond("CMP %", ">=", NUMBER, 1)],
    "Volume breakout": [
        cond("CMP", ">", "200 EMA (Weekly)"), cond("Volume", ">", "Avg Vol (30D)"),
        cond("CMP %", ">=", NUMBER, 2), cond("CMP", ">", "50 DMA")],
    "Pullback in uptrend": [
        cond("50 DMA", ">", "200 DMA"), cond("CMP", "<", "20 DMA"), cond("CMP", ">", "50 DMA"),
        cond("RSI 14", "<", NUMBER, 45)],
    "Within 2% of 50 DMA": [
        cond("CMP", ">=", "50 DMA", 2, "−%"), cond("CMP", "<=", "50 DMA", 2, "+%")],
    "Volume 2× average": [cond("Volume", ">=", "Avg Vol (30D)", 2, "×"), cond("CMP %", ">", NUMBER, 0)],
    "Empty (build your own)": [],
}
DEFAULT_PRESET = "My main query (default)"

# Dashboard: one box per market-cap group (2 x 2 grid), showing only BUY stocks
CAPS = ["Largecap", "Midcap", "Smallcap", "Microcap"]
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

DATA_DIR = Path(__file__).parent / "nse_data_v2"     # v2 = also stores previous close (for split adjustment)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TV_URL = "https://scanner.tradingview.com/india/scan"
TV_HEADERS = {"content-type": "application/json", "origin": "https://www.tradingview.com",
              "referer": "https://www.tradingview.com/", "user-agent": UA}
BHAV_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{:%Y%m%d}_F_0000.csv.zip"
# NSE's older file format (days before 8 Jul 2024), needed for the 4 years behind the 200-week average
OLD_BHAV_URL = "https://nsearchives.nseindia.com/content/historical/EQUITIES/{y}/{m}/cm{d}{m}{y}bhav.csv.zip"
UDIFF_START = dt.date(2024, 7, 8)


def bhav_url(day: dt.date) -> str:
    if day >= UDIFF_START:
        return BHAV_URL.format(day)
    m = day.strftime("%b").upper()
    return OLD_BHAV_URL.format(y=day.year, m=m, d=f"{day.day:02d}")
EQ_SERIES = {"EQ", "BE", "BZ", "SM", "ST"}

st.set_page_config(page_title="BUY Dashboard", page_icon="📊", layout="wide")

# ====================================================================== THEME
CAP_STYLE = {                       # icon, main colour, light colour (for each cap card)
    "Largecap": ("🏛️", "#4F46E5", "#818CF8"),
    "Midcap": ("🚀", "#0891B2", "#22D3EE"),
    "Smallcap": ("⚡", "#D97706", "#FBBF24"),
    "Microcap": ("🌱", "#DB2777", "#F472B6"),
}

st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
html, body, .stApp, .stMarkdown, button, input, textarea, select, label, p {font-family:'Inter',sans-serif;}
.hero, .hero *, .chips, .chips *, .kpis, .kpis *, .caps, .caps *, .sb-title, .sb-sub, .sb-h, .sb-sec {font-family:'Inter',sans-serif !important;}
.stApp {background:
   radial-gradient(1100px 520px at 0% -10%, #E0E7FF 0%, transparent 60%),
   radial-gradient(900px 480px at 100% -5%, #FCE7F3 0%, transparent 55%), #F8FAFC;}
header[data-testid="stHeader"] {background:transparent;}
.block-container {padding-top:1.3rem; max-width:1500px;}

/* ---------- hero ---------- */
.hero {position:relative; overflow:hidden; border-radius:22px; padding:26px 30px; color:#fff;
   background:linear-gradient(120deg,#1E1B4B 0%,#4338CA 42%,#7C3AED 72%,#DB2777 100%);
   box-shadow:0 22px 44px -18px rgba(67,56,202,.55); display:flex; justify-content:space-between;
   align-items:center; gap:18px;}
.hero:before, .hero:after {content:""; position:absolute; border-radius:50%; background:rgba(255,255,255,.08);}
.hero:before {width:280px; height:280px; right:-70px; top:-120px;}
.hero:after {width:180px; height:180px; right:180px; bottom:-120px;}
.hero h1 {color:#fff !important; margin:4px 0 4px; padding:0; font-size:2.05rem; font-weight:800; letter-spacing:-.02em;}
.hero p {margin:0; opacity:.85; font-size:.98rem;}
.eyebrow {font-size:.7rem; letter-spacing:.2em; font-weight:700; opacity:.75;}
.hero-r {display:flex; gap:8px; align-items:center; z-index:1; flex-wrap:wrap; justify-content:flex-end;}
.live {background:rgba(16,185,129,.18); border:1px solid rgba(52,211,153,.55); color:#D1FAE5;
   padding:6px 13px; border-radius:999px; font-weight:600; font-size:.82rem; white-space:nowrap;}
.dot {display:inline-block; width:8px; height:8px; border-radius:50%; background:#34D399; margin-right:7px;
   animation:pulse 1.8s infinite;}
@keyframes pulse {0%{box-shadow:0 0 0 0 rgba(52,211,153,.75)} 70%{box-shadow:0 0 0 9px rgba(52,211,153,0)}
   100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}}
.pill-glass {background:rgba(255,255,255,.14); border:1px solid rgba(255,255,255,.22); padding:6px 13px;
   border-radius:999px; font-size:.82rem; white-space:nowrap;}

/* ---------- query chips ---------- */
.chips {display:flex; flex-wrap:wrap; gap:7px; align-items:center; margin:4px 0;}
.chip-label {font-size:.75rem; color:#64748B; font-weight:700; text-transform:uppercase; letter-spacing:.08em; margin-right:4px;}
.chip {background:#fff; border:1px solid #E0E7FF; color:#3730A3; padding:5px 12px; border-radius:999px;
   font-size:.8rem; font-weight:600; box-shadow:0 1px 2px rgba(15,23,42,.06);}
.join {font-size:.66rem; color:#A5B4FC; font-weight:800;}

/* ---------- KPI cards ---------- */
.kpis {display:grid; grid-template-columns:repeat(var(--n,5),1fr); gap:14px; margin:8px 0 20px;}
@media (max-width:1100px) {.kpis {grid-template-columns:repeat(2,1fr);}}
.kpi {position:relative; overflow:hidden; background:#fff; border-radius:18px; padding:16px 18px 14px;
   border:1px solid #EEF2F7; box-shadow:0 10px 26px -16px rgba(15,23,42,.3);}
.kpi:before {content:""; position:absolute; left:0; top:0; bottom:0; width:5px; background:var(--c);}
.kpi-top {display:flex; align-items:center; gap:9px; color:#64748B; font-weight:600; font-size:.84rem;}
.kpi-ic {width:32px; height:32px; border-radius:10px; display:grid; place-items:center; font-size:1rem;
   background:color-mix(in srgb, var(--c) 14%, white);}
.kpi-val {font-size:2.1rem; font-weight:800; color:#0F172A; margin-top:8px; line-height:1; font-variant-numeric:tabular-nums;}
.kpi-sub {font-size:.76rem; color:#94A3B8; margin-top:5px;}
.kpi-bar {height:6px; background:#F1F5F9; border-radius:99px; margin-top:10px; overflow:hidden;}
.kpi-bar span {display:block; height:100%; border-radius:99px; background:linear-gradient(90deg,var(--c),var(--c2,var(--c)));}
.kpi.dark {background:linear-gradient(135deg,#0F172A 0%,#312E81 100%); border:none;}
.kpi.dark:before {background:linear-gradient(#34D399,#10B981);}
.kpi.dark .kpi-val {color:#fff;} .kpi.dark .kpi-top {color:#C7D2FE;} .kpi.dark .kpi-sub {color:#A5B4FC;}
.kpi.dark .kpi-ic {background:rgba(255,255,255,.12);}

/* ---------- cap cards ---------- */
.caps {display:grid; grid-template-columns:1fr 1fr; gap:18px;}
@media (max-width:1000px) {.caps {grid-template-columns:1fr;}}
.cap {background:#fff; border-radius:20px; border:1px solid #EEF2F7; overflow:hidden;
   box-shadow:0 14px 34px -20px rgba(15,23,42,.4); transition:transform .15s ease, box-shadow .15s ease;}
.cap:hover {transform:translateY(-2px); box-shadow:0 20px 40px -20px rgba(15,23,42,.45);}
.cap-head {display:flex; justify-content:space-between; align-items:center; padding:16px 20px; color:#fff;
   background:linear-gradient(120deg,var(--c),var(--c2));}
.cap-title {font-size:1.15rem; font-weight:800; letter-spacing:-.01em;}
.cap-sub {font-size:.74rem; opacity:.88; margin-top:2px;}
.cap-count {text-align:center; background:rgba(255,255,255,.2); border:1px solid rgba(255,255,255,.4);
   border-radius:14px; padding:5px 14px; line-height:1.05;}
.cap-count b {display:block; font-size:1.45rem; font-weight:800;}
.cap-count small {font-size:.62rem; letter-spacing:.12em; opacity:.9;}
.cap-body {max-height:430px; overflow-y:auto;}
.cap-body::-webkit-scrollbar {width:8px;} .cap-body::-webkit-scrollbar-thumb {background:#E2E8F0; border-radius:8px;}
table.tbl {width:100%; border-collapse:collapse; font-size:.88rem; margin:0; display:table;}
table.tbl, table.tbl th, table.tbl td {border:none !important;}
table.tbl thead th {position:sticky; top:0; z-index:1; background:#F8FAFC; color:#64748B; font-weight:700;
   font-size:.68rem; text-transform:uppercase; letter-spacing:.08em; padding:10px 20px; text-align:left;
   border-bottom:1px solid #EEF2F7 !important;}
table.tbl td {padding:9px 20px; border-bottom:1px solid #F1F5F9 !important; color:#0F172A;}
table.tbl tbody tr:hover td {background:#F8FAFF;}
table.tbl .r {text-align:right;}
tr.sec td {background:color-mix(in srgb, var(--c) 8%, white); color:var(--c); font-weight:800;
   font-size:.74rem; letter-spacing:.04em; text-transform:uppercase; padding:7px 20px;}
.sec-n {background:#fff; color:#475569; border:1px solid #E2E8F0; border-radius:99px; padding:1px 8px;
   margin-left:6px; font-size:.68rem; font-weight:700;}
.sym {font-weight:700; letter-spacing:.01em;}
.price {color:#475569; font-variant-numeric:tabular-nums;}
.pct {display:inline-block; min-width:84px; text-align:center; padding:4px 10px; border-radius:999px;
   font-weight:700; font-size:.8rem; font-variant-numeric:tabular-nums;}
.empty {padding:38px 20px; text-align:center; color:#94A3B8; font-weight:600;}
.empty span {display:block; font-size:2rem; margin-bottom:6px;}

/* ---------- buttons / table / sidebar ---------- */
.stButton button[kind="primary"], .stDownloadButton button {background:linear-gradient(90deg,#6366F1,#A855F7,#EC4899);
   border:none; color:#fff; font-weight:700; border-radius:12px; box-shadow:0 8px 20px -10px rgba(99,102,241,.8);}
.stButton button[kind="primary"]:hover, .stDownloadButton button:hover {filter:brightness(1.07); color:#fff;}
div[data-testid="stDataFrame"] {background:#fff; border-radius:16px; padding:6px; border:1px solid #EEF2F7;
   box-shadow:0 10px 26px -18px rgba(15,23,42,.35);}

section[data-testid='stSidebar'] {width:600px !important; background:linear-gradient(180deg,#0F172A 0%,#1E1B4B 100%);}
section[data-testid='stSidebar'] [data-testid='stVerticalBlock'] {gap:0.4rem;}
section[data-testid='stSidebar'] div[data-testid='stHorizontalBlock'] {gap:0.35rem;}
section[data-testid='stSidebar'] h1, section[data-testid='stSidebar'] h2, section[data-testid='stSidebar'] h3,
section[data-testid='stSidebar'] [data-testid='stWidgetLabel'] p, section[data-testid='stSidebar'] .stMarkdown p,
section[data-testid='stSidebar'] [data-testid='stCaptionContainer'], section[data-testid='stSidebar'] label {color:#E2E8F0 !important;}
section[data-testid='stSidebar'] div[data-baseweb="select"] > div {background:#fff; border-radius:10px; border:none; padding-left:4px;}
section[data-testid='stSidebar'] div[data-baseweb="select"] > div > div:first-child {padding-left:6px; padding-right:0;}
section[data-testid='stSidebar'] div[data-baseweb="select"] * {color:#0F172A !important; -webkit-text-fill-color:#0F172A;}
section[data-testid='stSidebar'] [data-testid='stNumberInput'] input,
section[data-testid='stSidebar'] [data-testid='stTextInput'] input {background:#fff; color:#0F172A; border-radius:10px;}
section[data-testid='stSidebar'] input:disabled {background:rgba(255,255,255,.05) !important; color:#475569 !important; -webkit-text-fill-color:#475569;}
section[data-testid='stSidebar'] [data-testid='stNumberInput'] div:has(> input:disabled) {background:rgba(255,255,255,.06) !important; border-color:rgba(255,255,255,.08) !important;}
section[data-testid='stSidebar'] div[data-baseweb="select"][aria-disabled="true"] > div {background:rgba(255,255,255,.12);}
section[data-testid='stSidebar'] .stButton button {background:rgba(255,255,255,.07); border:1px solid rgba(255,255,255,.16);
   color:#F8FAFC; border-radius:10px; padding:0.15rem 0.5rem; min-height:2.35rem;}
section[data-testid='stSidebar'] .stButton button:hover {border-color:#A5B4FC; color:#fff; background:rgba(165,180,252,.12);}
section[data-testid='stSidebar'] .stButton button[kind="primary"] {background:linear-gradient(90deg,#6366F1,#EC4899); border:none;}
section[data-testid='stSidebar'] hr {border-color:rgba(255,255,255,.12);}
.sb-title {font-size:1.35rem; font-weight:800; color:#fff; letter-spacing:-.01em; margin-top:-8px;}
.sb-sub {font-size:.8rem; color:#A5B4FC; margin:2px 0 10px;}
.sb-h {font-size:.66rem; font-weight:800; letter-spacing:.12em; text-transform:uppercase; color:#A5B4FC;}
.sb-sec {font-size:.72rem; font-weight:800; letter-spacing:.14em; text-transform:uppercase; color:#818CF8; margin:8px 0 2px;}
</style>""", unsafe_allow_html=True)


def esc(x) -> str:
    return html.escape(str(x))


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
@st.cache_data(ttl=REFRESH_SEC - 20, show_spinner="Loading Google Sheet…")
def load_sheet() -> pd.DataFrame:
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
    df = pd.read_csv(url, dtype=str, keep_default_na=False)
    df.columns = [" ".join(str(c).split()) for c in df.columns]
    df = df.loc[:, ~df.columns.str.startswith("Unnamed")]
    df = df.apply(lambda c: c.str.strip())
    df = df.replace({"": pd.NA, "#N/A": pd.NA, "N/A": pd.NA})
    df = df.dropna(how="all").dropna(axis=1, how="all")
    df = df.drop(columns=["Close"], errors="ignore")

    # ---- sheet columns -> app names, so the app uses the SHEET'S OWN numbers first
    rename = {"RSI": "RSI 14", "30": "Avg Vol (30D)", "60": "Avg Vol (60D)", "90": "Avg Vol (90D)"}
    for c in df.columns:
        m = re.fullmatch(r"(\d+) EMA \(weekly\)", c, flags=re.I)
        if m:
            rename[c] = f"{m.group(1)} EMA (Weekly)"
    df = df.rename(columns=rename)
    df = df.drop(columns=[c for c in df.columns                      # only 200 EMA (weekly) is used
                          if re.fullmatch(r"\d+ EMA \(Weekly\)", c) and c not in WEEKLY_DMA])

    # ---- right-hand block (Symbol | Market Cap | cmp | Sec): matched by ITS OWN Symbol column,
    #      so it stays correct even if that block is sorted or filtered differently
    right_sym = next((c for c in df.columns if re.fullmatch(r"Symbol\.\d+", c)), None)
    # look only to the RIGHT of that block's Symbol column, so "cmp" (% change) is never confused
    # with the main "CMP" (price) column
    after = list(df.columns[df.columns.get_loc(right_sym) + 1:]) if right_sym else list(df.columns)
    pct_col = next((c for c in after if c != "CMP" and c.lower() in ("cmp", "cmp %", "cmp%", "change %")), None)
    sec_col = next((c for c in after if c.lower() in ("sec", "sector", "sectors")), None)
    if right_sym:
        block = df[[c for c in [right_sym, "Market Cap", pct_col, sec_col] if c]].copy()
        block["_k"] = block[right_sym].fillna("").str.upper().str.strip()
        block = block[block["_k"] != ""].drop_duplicates("_k").set_index("_k")
        own = df["Symbol"].fillna("").str.upper().str.strip()
        if pct_col:
            df["CMP %"] = own.map(block[pct_col])
        if sec_col:
            df["Sector"] = own.map(block[sec_col])
        if "Market Cap" in block and "M Cap" in df.columns:
            df["M Cap"] = df["M Cap"].fillna(own.map(block["Market Cap"]))   # M Cap cells showing #N/A
    else:
        df = df.rename(columns={k: v for k, v in {pct_col: "CMP %", sec_col: "Sector"}.items() if k})
    df = df.drop(columns=[c for c in df.columns if c not in ("CMP %", "Sector") and
                          (re.fullmatch(r"(Output|Symbol)(\.\d+)?|Market Cap|Output 2|cmp|Sec", c))
                          and c != "Symbol"])

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
    prev = pick("PrvsClsgPric", "PREVCLOSE")       # NSE adjusts this on split / bonus ex-dates
    df = df[df[ser].astype(str).str.strip().isin(EQ_SERIES)]
    out = pd.DataFrame({"key": df[sym].astype(str).str.strip().map(match_key),
                        "close": pd.to_numeric(df[close], errors="coerce"),
                        "prev": pd.to_numeric(df[prev], errors="coerce"),
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
            r = session.get(bhav_url(day), timeout=20)
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
    """Download any missing NSE files for the last year. Returns the days that FAILED.
    Each file is downloaded only once; after the first run only the newest day is fetched."""
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
    """Per stock, from the saved NSE daily files:
        20/50/100/200 DMA and 30/60/90-day average volume.
    Prices are adjusted for splits / bonuses: on an ex-date NSE reports an adjusted "previous close",
    so  factor = previous close ÷ last actual close,  and every earlier close is multiplied by the
    product of all later factors (same idea as Google Finance / TradingView adjusted charts).
    `files` is in the cache key, so new downloads are picked up automatically."""
    longest = max(DMA_LOOKBACK_DAYS, *AVG_VOL.values())
    frames = {}
    for f in DATA_DIR.glob("*.csv"):
        try:
            d = dt.datetime.strptime(f.stem, "%Y%m%d").date()
        except ValueError:
            continue
        if day - dt.timedelta(days=longest) <= d < day:
            frames[d] = pd.read_csv(f).set_index("key")
    if not frames:
        raise RuntimeError("No NSE data downloaded yet")

    days = sorted(frames)                                                  # oldest → newest
    closes = pd.DataFrame({d: frames[d]["close"] for d in days})           # rows = stocks, cols = days
    prevs = pd.DataFrame({d: frames[d].get("prev") for d in days})
    volumes = pd.DataFrame({d: frames[d]["volume"] for d in days})

    # ---- split / bonus adjustment
    last_close = closes.ffill(axis=1).shift(1, axis=1)                    # previous traded close
    factor = prevs / last_close
    factor = factor.where(((factor - 1).abs() > ADJ_TOLERANCE) & (factor > 0), 1.0).fillna(1.0)
    later = factor.iloc[:, ::-1].cumprod(axis=1).iloc[:, ::-1].shift(-1, axis=1).fillna(1.0)
    adj = closes * later

    stats = pd.DataFrame(index=closes.index)

    # DMA = AVERAGE(QUERY(SORT(GOOGLEFINANCE(.., "Price", TODAY()-365, TODAY()), 1, 0), "Select Col2 limit N"))
    newest_first = days[::-1]
    in_year = [d for d in newest_first if d >= day - dt.timedelta(days=DMA_LOOKBACK_DAYS)]
    long = adj[in_year].stack()                            # (stock, day) newest first, gaps dropped
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
        data[col] = num(data[col]) if col in data.columns else None    # keep the sheet's numbers
    st.error(f"NSE refused {len(failed)} daily file(s) (e.g. {failed[0]:%d %b %Y}), so DMA and "
             "Avg Vol are hidden instead of showing wrong numbers. Press **🔄 Reload** in a minute to retry.")
else:
    if failed:
        st.warning("These days couldn't be downloaded from NSE (holiday or refused): "
                   + ", ".join(f"{d:%d %b}" for d in failed) + ". Press **🔄 Reload** later to retry.")
    try:
        stats = nse_stats(today, len(list(DATA_DIR.glob("*"))))
        for col in STAT_COLS:          # the sheet's own value wins; NSE only fills blanks
            sheet_val = num(data[col]) if col in data.columns else pd.Series(float("nan"), index=data.index)
            data[col] = sheet_val.fillna(data["_key"].map(stats[col]))
    except Exception as e:
        for col in STAT_COLS:
            data[col] = num(data[col]) if col in data.columns else None
        st.warning(f"Couldn't calculate DMA / average volume: {e}")

# OHLC: use the sheet's number; when the sheet has no number ("Data not available", #N/A, blank)
# or the stock isn't in the sheet, calculate it from NSE: (Open+High+Low+Close)/4 of the previous day
if "OHLC" not in data.columns:
    data["OHLC"] = pd.NA
need_ohlc = data["_extra"] | num(data["OHLC"]).isna()
if need_ohlc.any():
    try:
        ohlc = prev_day_ohlc(today)
        data.loc[need_ohlc, "OHLC"] = data.loc[need_ohlc, "_key"].map(ohlc).map(
            lambda v: f"₹{v:,.2f}" if pd.notna(v) else pd.NA)
    except Exception as e:
        st.warning(f"OHLC is missing in the sheet and couldn't be calculated from NSE: {e}")

# ====================================================================== SIDEBAR (screener)

OP_WORDS = {">": "is above", ">=": "is at least", "<": "is below", "<=": "is at most", "=": "equals"}
VS_NUMBER = "a number…"                    # shown in the "than" box when comparing with a typed value


def new_id() -> int:
    st.session_state["next_id"] = st.session_state.get("next_id", 0) + 1
    return st.session_state["next_id"]


def to_draft(rule_list):
    """Preset / applied rules -> editable draft rows (each with its own id for the widgets)."""
    return [{"id": new_id(), "Field": r["Field"], "Op": r["Op"], "Compare to": r["Compare to"],
             "Math": r.get("Math", ""), "Number": r.get("Number")} for r in rule_list]


if "rules" not in st.session_state:                       # first visit: default sheet rule
    st.session_state["rules"] = [{k: {"Math": ""}.get(k) if k not in r else r[k]
                                  for k in ("Field", "Op", "Compare to", "Math", "Number")}
                                 for r in PRESETS[DEFAULT_PRESET]]
    st.session_state["draft"] = to_draft(st.session_state["rules"])
    st.session_state["match"] = "ALL"


def load_preset():
    st.session_state["draft"] = to_draft(PRESETS[st.session_state["preset"]])


def add_condition():
    st.session_state["draft"].append({"id": new_id(), "Field": "CMP", "Op": ">",
                                      "Compare to": "20 DMA", "Math": "", "Number": None})


def delete_condition(rid):
    st.session_state["draft"] = [r for r in st.session_state["draft"] if r["id"] != rid]


def apply_conditions():
    st.session_state["rules"] = [{k: r[k] for k in ("Field", "Op", "Compare to", "Math", "Number")}
                                 for r in st.session_state["draft"]]
    st.session_state["match"] = st.session_state["match_choice"]


def right_text(r: dict) -> str:
    """Right-hand side in words: '70', '20 DMA', '200 EMA (Weekly) × 1.01', '50 DMA + 2 %'."""
    n = r.get("Number")
    n_txt = f"{n:g}" if n is not None and pd.notna(n) else "?"
    if r["Compare to"] == NUMBER:
        return n_txt
    m = r.get("Math") or ""
    if not m:
        return r["Compare to"]
    return f"{r['Compare to']} {MATH[m].replace(' %', '')} {n_txt}{' %' if m.endswith('%') else ''}"


def sentence(r: dict) -> str:
    return f"{r['Field']} {OP_WORDS.get(r['Op'], r['Op'])} {right_text(r)}"


sb = st.sidebar
sb.markdown('<div class="sb-title">🧮 Stock Screener</div>'
            '<div class="sb-sub">Build your BUY rule, then press Apply</div>', unsafe_allow_html=True)

# ① preset + ③ AND/OR on one line
p1, p2 = sb.columns([3, 2])
p1.selectbox("Start from", list(PRESETS), key="preset", on_change=load_preset)
p2.selectbox("Stock must meet", ["ALL", "ANY"], index=["ALL", "ANY"].index(st.session_state["match"]),
             key="match_choice",
             format_func=lambda m: "all conditions (AND)" if m == "ALL" else "any condition (OR)")

# ② conditions: ONE compact line each →  [field] [op] [than] [math] [value] [🗑]
SYM = {">": ">", ">=": "≥", "<": "<", "<=": "≤", "=": "="}
WIDTHS = [2.35, 1.3, 2.55, 1.3, 1.35, 0.7]
sb.markdown('<div class="sb-sec">Conditions</div>', unsafe_allow_html=True)
h = sb.columns(WIDTHS)
for col, label in zip(h, ["If", "is", "than", "math", "value", ""]):
    col.markdown(f'<span class="sb-h">{label}</span>', unsafe_allow_html=True)

for r in st.session_state["draft"]:
    rid = r["id"]
    c1, c2, c3, c4, c5, c6 = sb.columns(WIDTHS, vertical_alignment="center")
    r["Field"] = c1.selectbox("If", FIELDS, index=FIELDS.index(r["Field"]), key=f"f_{rid}",
                              label_visibility="collapsed")
    r["Op"] = c2.selectbox("is", list(SYM), index=list(SYM).index(r["Op"]), format_func=SYM.get,
                           key=f"o_{rid}", label_visibility="collapsed")
    options = [VS_NUMBER, *FIELDS]
    current = VS_NUMBER if r["Compare to"] == NUMBER else r["Compare to"]
    pick = c3.selectbox("than", options, index=options.index(current), key=f"c_{rid}",
                        label_visibility="collapsed")
    r["Compare to"] = NUMBER if pick == VS_NUMBER else pick
    vs_field = r["Compare to"] != NUMBER
    r["Math"] = c4.selectbox("math", list(MATH), index=list(MATH).index(r.get("Math") or ""),
                             format_func=MATH.get, key=f"m_{rid}", label_visibility="collapsed",
                             disabled=not vs_field,
                             help="× ÷ + − a number, or + % / − % (e.g. + 2 % = 2 % above)")
    if not vs_field:
        r["Math"] = ""
    needs_number = (not vs_field) or bool(r["Math"])
    val = c5.number_input("value", value=float(r["Number"] if r["Number"] is not None else 0),
                          step=0.01 if r["Math"] in ("×", "÷") else 1.0, format="%g",
                          key=f"n_{rid}", label_visibility="collapsed", disabled=not needs_number)
    r["Number"] = val if needs_number else None
    c6.button("🗑️", key=f"del_{rid}", help=f"{sentence(r)} — click to remove",
              on_click=delete_condition, args=(rid,))

if not st.session_state["draft"]:
    sb.caption("No conditions yet — every stock would count as BUY.")

pending = ([{k: r[k] for k in ("Field", "Op", "Compare to", "Math", "Number")} for r in st.session_state["draft"]]
           != st.session_state["rules"] or st.session_state["match_choice"] != st.session_state["match"])
b1, b2 = sb.columns(2)
b1.button("➕ Add condition", on_click=add_condition, width="stretch")
b2.button("✅ Apply" + (" •" if pending else ""), type="primary", width="stretch",
          on_click=apply_conditions, help="Changes not applied yet" if pending else "Up to date")
if pending:
    sb.caption("🟡 You have changes that are not applied yet — press **Apply**.")

rules, match_mode = st.session_state["rules"], st.session_state["match"]

show_all = st.session_state.get("show_all", False)

view = data
only_buy = False
if show_all:                         # these filters only matter on the "all calculated data" page
    st.sidebar.markdown('<div class="sb-sec">Table filters</div>', unsafe_allow_html=True)
    if "M Cap" in data.columns:
        caps = st.sidebar.multiselect("M Cap", sorted(data["M Cap"].dropna().unique()), placeholder="All")
        if caps:
            view = view[view["M Cap"].isin(caps)]
    search = st.sidebar.text_input("Search symbol", placeholder="e.g. RELIANCE")
    if search:
        view = view[view["_sym"].str.contains(search, case=False, na=False)]
    only_buy = st.sidebar.checkbox("Show only BUY")
st.sidebar.divider()
if st.sidebar.button("🔄 Reload data", width="stretch"):
    st.cache_data.clear()
    st.rerun()


def toggle_view():
    st.session_state["show_all"] = not st.session_state.get("show_all", False)


def hero(title: str, subtitle: str):
    st.markdown(
        f'<div class="hero"><div style="z-index:1"><div class="eyebrow">NSE · LIVE STOCK SCREENER</div>'
        f'<h1>{esc(title)}</h1><p>{esc(subtitle)}</p></div>'
        f'<div class="hero-r"><span class="live"><span class="dot"></span>Live</span>'
        f'<span class="pill-glass">⟳ every {REFRESH_SEC // 60} min</span>'
        f'<span class="pill-glass">{len(data):,} stocks</span></div></div>', unsafe_allow_html=True)


def query_chips(rules: list, match_mode: str) -> str:
    active = [r for r in rules if r.get("On", True)]
    if not active:
        return '<div class="chips"><span class="chip-label">Active query</span><span class="chip">no conditions — every stock is BUY</span></div>'
    join = '<span class="join">AND</span>' if match_mode == "ALL" else '<span class="join">OR</span>'
    chips = join.join(f'<span class="chip">{esc(r["Field"])} {esc(SYM.get(r["Op"], r["Op"]))} {esc(right_text(r))}</span>'
                      for r in active)
    return f'<div class="chips"><span class="chip-label">Active query</span>{chips}</div>'


if show_all:
    hero("All Calculated Data", "Every stock with its sheet values, moving averages, RSI and BUY / AVOID signal")
else:
    hero("BUY Dashboard", "Today's BUY signals across Largecap · Midcap · Smallcap · Microcap")
st.write("")
bar_l, bar_r = st.columns([4.4, 1.6], vertical_alignment="center")
bar_l.markdown(query_chips(rules, match_mode), unsafe_allow_html=True)
bar_r.button("⬅️ Back to dashboard" if show_all else "📋 Show all calculated data",
             on_click=toggle_view, type="primary", width="stretch")


# ====================================================================== SIGNAL
def rule_text(r: dict) -> str:
    return sentence(r)


def query_text(rules: list, match_mode: str) -> str:
    active = [rule_text(r) for r in rules if r.get("On", True)]
    if not active:
        return "No conditions set — every stock counts as BUY"
    joiner = " AND " if match_mode == "ALL" else " OR "
    return "BUY when " + joiner.join(active)


def add_signal(t: pd.DataFrame, rules: list, match_mode: str) -> pd.DataFrame:
    """Output = BUY when the stock passes ALL (or ANY) of the switched-on rules.
    A rule that needs a missing number (blank OHLC, no RSI, ...) counts as not passed."""
    values = {f: (num(t[f]) if f in t.columns else pd.Series(float("nan"), index=t.index)) for f in FIELDS}
    ops = {">": pd.Series.gt, ">=": pd.Series.ge, "<": pd.Series.lt, "<=": pd.Series.le, "=": pd.Series.eq}

    results = []
    for r in rules:
        if not r.get("On", True) or r.get("Field") not in values or r.get("Op") not in ops:
            continue
        left = values[r["Field"]]
        n = r.get("Number")
        n = None if n is None or pd.isna(n) else float(n)
        if r["Compare to"] == NUMBER:
            if n is None:
                continue                                   # no number typed yet: skip the row
            right = n
        else:
            right = values.get(r["Compare to"])
            if right is None:
                continue
            m = r.get("Math") or ""
            if m:
                if n is None:
                    continue
                right = {"×": right * n, "÷": right / n if n else right * float("nan"),
                         "+": right + n, "−": right - n,
                         "+%": right * (1 + n / 100), "−%": right * (1 - n / 100)}[m]
        results.append(ops[r["Op"]](left, right).fillna(False))

    if not results:
        buy = pd.Series(True, index=t.index)
    elif match_mode == "ANY":
        buy = pd.concat(results, axis=1).any(axis=1)
    else:
        buy = pd.concat(results, axis=1).all(axis=1)
    t["Output"] = buy.map({True: "BUY", False: "AVOID"})
    D, E = num(t["OHLC"]), num(t["CMP"])
    t["_ohlc_bad"] = (((D - E).abs() / E) > OHLC_SANITY).fillna(False)   # sheet OHLC error check
    return t


def style(t: pd.DataFrame):
    """Whole row red for BUY; OHLC cell yellow when it looks wrong."""
    red = "background-color: #ffcdd2; color: #b71c1c; font-weight: 600"
    yellow = "background-color: #fff59d; color: #6d4c00; font-weight: 600"
    fmt = {**{c: "{:.2f}" for c in [*RSI_FIELDS, *SHOW_MA, "CMP %"]},
           **{c: "{:,.0f}" for c in AVG_VOL}}
    t = t.copy()
    bad = t.pop("_ohlc_bad")
    for c in t.columns:                                  # blank sheet cells: show empty, not "None"
        if t[c].dtype == object:
            t[c] = t[c].where(t[c].notna(), "")

    def row_style(row):
        css = [red if row["Output"] == "BUY" else ""] * len(row)
        if bad[row.name] and "OHLC" in row.index:
            css[row.index.get_loc("OHLC")] = yellow
        return css

    return (t.style
            .apply(row_style, axis=1)
            .format({k: v for k, v in fmt.items() if k in t.columns}, na_rep=""))


# ====================================================================== DASHBOARD (2 x 2 boxes)
def kpi_card(icon, name, value, sub, color, color2=None, pct=None, dark=False) -> str:
    bar = (f'<div class="kpi-bar"><span style="width:{max(0, min(pct, 100)):.1f}%"></span></div>'
           if pct is not None else "")
    return (f'<div class="kpi{" dark" if dark else ""}" style="--c:{color};--c2:{color2 or color}">'
            f'<div class="kpi-top"><span class="kpi-ic">{icon}</span>{esc(name)}</div>'
            f'<div class="kpi-val">{value}</div><div class="kpi-sub">{sub}</div>{bar}</div>')


def pct_pill(v, biggest) -> str:
    """CMP % as a coloured pill: green ▲ up, red ▼ down, stronger colour = bigger move."""
    if pd.isna(v):
        return '<span class="pct" style="background:#F1F5F9;color:#94A3B8">—</span>'
    a = 0.12 + 0.5 * min(abs(v) / biggest, 1) if biggest else 0.25
    if v >= 0:
        return f'<span class="pct" style="background:rgba(22,163,74,{a:.2f});color:#14532D">▲ {v:.2f}%</span>'
    return f'<span class="pct" style="background:rgba(220,38,38,{a:.2f});color:#7F1D1D">▼ {abs(v):.2f}%</span>'


def cap_card(cap: str, part: pd.DataFrame, has_sector: bool) -> str:
    icon, c1, c2 = CAP_STYLE.get(cap, ("📈", "#334155", "#64748B"))
    head = (f'<div class="cap-head"><div><div class="cap-title">{icon} {esc(cap)}</div>'
            f'<div class="cap-sub">BUY signals{" · grouped by sector" if has_sector else ""} · sorted by CMP %</div></div>'
            f'<div class="cap-count"><b>{len(part)}</b><small>BUY</small></div></div>')
    if part.empty:
        return (f'<div class="cap" style="--c:{c1};--c2:{c2}">{head}'
                f'<div class="empty"><span>😴</span>No BUY signals right now</div></div>')
    biggest = part["CMP %"].abs().max()
    rows = []
    for sector, grp in part.groupby("Sector", sort=False):
        if has_sector:
            rows.append(f'<tr class="sec"><td colspan="3">{esc(sector)}<span class="sec-n">{len(grp)}</span></td></tr>')
        for _, r in grp.iterrows():
            price = num(pd.Series([r["CMP"]])).iloc[0]
            price_txt = f"₹{price:,.2f}" if pd.notna(price) else "—"
            rows.append(f'<tr><td class="sym">{esc(r["Symbol"])}</td><td class="r price">{price_txt}</td>'
                        f'<td class="r">{pct_pill(r["CMP %"], biggest)}</td></tr>')
    table = ('<table class="tbl"><thead><tr><th>Symbol</th><th class="r">CMP</th><th class="r">CMP %</th>'
             f'</tr></thead><tbody>{"".join(rows)}</tbody></table>')
    return f'<div class="cap" style="--c:{c1};--c2:{c2}">{head}<div class="cap-body">{table}</div></div>'


def dashboard(table: pd.DataFrame, now, rule: str):
    """KPI cards + four cap cards (Largecap, Midcap, Smallcap, Microcap), each listing only its BUY
    stocks grouped by sector and sorted by CMP % (biggest gainer first)."""
    caps_all = table["M Cap"].fillna("").str.strip().str.lower() if "M Cap" in table.columns else pd.Series("", index=table.index)
    buy_mask = table["Output"] == "BUY"
    buys = table[buy_mask].copy()
    caps = caps_all[buy_mask]
    if "Sector" not in buys.columns:
        buys["Sector"] = ""
    buys["Sector"] = buys["Sector"].fillna("").replace({"#N/A": ""})
    has_sector = buys["Sector"].ne("").any()
    buys["Sector"] = buys["Sector"].replace({"": "Other"})
    buys["_other"] = buys["Sector"].eq("Other")                    # "Other" goes last in each card

    # ---- KPI row
    total = len(table)
    cards = [kpi_card("🎯", "Total BUY", len(buys),
                      f"of {total:,} stocks · updated {now:%I:%M %p}", "#10B981",
                      pct=100 * len(buys) / total if total else 0, dark=True)]
    for cap in CAPS:
        icon, c1, c2 = CAP_STYLE[cap]
        n_cap = int((caps_all == cap.lower()).sum())
        n_buy = int((caps == cap.lower()).sum())
        share = 100 * n_buy / n_cap if n_cap else 0
        cards.append(kpi_card(icon, cap, n_buy, f"of {n_cap:,} stocks · {share:.1f}%", c1, c2, pct=share))
    st.markdown(f'<div class="kpis" style="--n:{len(cards)}">{"".join(cards)}</div>', unsafe_allow_html=True)

    # ---- 2 x 2 cap cards
    html_cards = []
    for cap in CAPS:
        part = (buys.loc[caps == cap.lower(), ["_other", "Sector", "Symbol", "CMP", "CMP %"]]
                .sort_values(["_other", "Sector", "CMP %"], ascending=[True, True, False])
                .drop(columns="_other"))
        html_cards.append(cap_card(cap, part, has_sector))
    st.markdown(f'<div class="caps">{"".join(html_cards)}</div>', unsafe_allow_html=True)


# ====================================================================== LIVE TABLE
@st.fragment(run_every=REFRESH_SEC)
def live_table():
    # Timer tick (every REFRESH_SEC): reload the WHOLE page, so the Google Sheet is read again too
    if time.time() - st.session_state.get("_page_loaded", 0) > REFRESH_SEC - 30:
        st.rerun()
    now = pd.Timestamp.now(tz="Asia/Kolkata")
    table = data.copy()                       # calculate for ALL stocks; filters applied below
    tv_cols = {**RSI_FIELDS, **WEEKLY_DMA, "_tv_close": "close", "_tv_volume": "volume",
               "_tv_change": "change"}
    try:
        tv = fetch_tv(tv_cols)
        for col in tv_cols:
            live = table["_key"].map(tv[col])
            if col in table.columns and not col.startswith("_"):
                table[col] = num(table[col]).fillna(live)        # sheet's number wins
            else:
                table[col] = live
    except Exception as e:
        for col in tv_cols:
            table[col] = num(table[col]) if col in table.columns else float("nan")
        st.warning(f"Couldn't get RSI from TradingView: {e}")

    # Extra stocks (not in the sheet): live CMP and Volume from TradingView
    ex = table["_extra"]
    if ex.any():
        table.loc[ex, "CMP"] = table.loc[ex, "_tv_close"].map(lambda v: f"₹{v:,.2f}" if pd.notna(v) else pd.NA)
        if "Volume" in table.columns:
            table.loc[ex, "Volume"] = table.loc[ex, "_tv_volume"].map(lambda v: f"{v:.0f}" if pd.notna(v) else pd.NA)
    for col in STAT_COLS:
        table[col] = pd.to_numeric(table[col], errors="coerce")

    # CMP % (today's change) from the Google Sheet's "cmp" column; TradingView only where it's blank
    if "CMP %" not in table.columns:
        table["CMP %"] = pd.NA
    table["CMP %"] = num(table["CMP %"]).fillna(table["_tv_change"]).round(2)

    table = add_signal(table, rules, match_mode)
    rule = query_text(rules, match_mode)

    if not show_all:
        dashboard(table, now, rule)
        return
    table = table.loc[table.index.isin(view.index)]   # sidebar filters (M Cap / search)

    bad_syms = table.loc[table["_ohlc_bad"], "_sym"].tolist()
    if bad_syms:
        st.warning(f"OHLC in the sheet looks wrong (more than {OHLC_SANITY:.0%} away from CMP) for: "
                   + ", ".join(bad_syms) + ". These show AVOID only because of that cell — "
                   "copy the OHLC formula from a working row into them.")

    n_buy = int((table["Output"] == "BUY").sum())
    cards = [kpi_card("📋", "Stocks in view", f"{len(table):,}", f"updated {now:%I:%M %p}", "#6366F1", "#A855F7", dark=True),
             kpi_card("🎯", "BUY", n_buy, f"{100 * n_buy / len(table):.1f}% of view" if len(table) else "—",
                      "#10B981", "#34D399", pct=100 * n_buy / len(table) if len(table) else 0),
             kpi_card("⚠️", "Bad OHLC cells", len(bad_syms), "OHLC far away from CMP in the sheet", "#F59E0B", "#FBBF24")]
    st.markdown(f'<div class="kpis" style="--n:3">{"".join(cards)}</div>', unsafe_allow_html=True)

    if only_buy:
        table = table[table["Output"] == "BUY"]

    # Output at the end
    cols = [c for c in table.columns
            if c != "Output" and (not c.startswith("_") or c == "_ohlc_bad")]
    for col, after in (("CMP %", "CMP"), ("200 EMA (Weekly)", "200 DMA")):   # keep related columns together
        if col in cols and after in cols:
            cols.remove(col)
            cols.insert(cols.index(after) + 1, col)
    table = table[[*cols, "Output"]]

    st.dataframe(style(table), hide_index=True, width="stretch", height=650)
    st.download_button("⬇️ Download CSV",
                       table.drop(columns="_ohlc_bad").to_csv(index=False).encode("utf-8-sig"),
                       "stock_list.csv", "text/csv")


st.session_state["_page_loaded"] = time.time()
live_table()