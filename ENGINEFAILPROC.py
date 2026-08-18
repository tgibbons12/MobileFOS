


def get_airport_specific_altitudes(icao_code, max_elevation):
    """
    Return special THR RED / ACC ALT and EO ACC values for specific airports.
    If no exception exists, returns the default calculated values.
    
    Args:
        icao_code: The ICAO code of the airport
        max_elevation: The maximum elevation to use for default calculations
    
    Returns:
        Dictionary with thr_red, acc, and eo_acc values
    """
    # Dictionary of airport-specific altitude exceptions
    # Format: {icao_code: {'thr_red': value, 'acc': value, 'eo_acc': value}}
    airport_exceptions = {
    'KABQ': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURES EXIST\nRWYS 03 / 08 / 26 /\nRUNWAY 12/30 PROHIBITED FOR TAKEOFF. OK FOR TAXI.'
    },
    'KACV': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RW14: TRK RCL, AT D3.0 ACV RT H-250.'
    },
    'PANC': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'REF 10-7 PAGES'
    },
    'NFSA': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RW08: TRK RCL TO 2.0 DME FA VOR, LT H-320 TO INCPT FA (VOR) R-001 OUTBOUND. RW26: TRK RCL TO 5.0 DME FA VOR, RT H-320 TO INCPT FA (VOR) R-001 OUTBOUND'
    },
    'KASE': {
        'thr_red': "3000",
        'acc': "3000",
        'eo_acc': "3000",
        'EFP': 'REF 10-7 PAGES'
    },
    'TNCA': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1500",
        'EFP': 'RW11: TRK RCL TO D2.7 BEA VOR RT (15 DEG BANK, MAX 190 KTS) H-210. RW29: TRK RCL (SHIPS UP TO 302 FT CONSIDERED)'
    },
    'KAVL': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RW17: DT BRA NDB. RW35: TRK RCL TO IM NDB, RT H-020 TO INTCP SUG R-313, TRK DCT JUNOE.'
    },
    'LEBL': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "3000",
        'EFP': 'RW02: TRK RCL TO 2.5 DME PRA VOR, RT H-110.'
    },
    'SKBO': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1500",
        'EFP': 'REF 10-7 PAGES'
    },
    'KBOI': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RW10L/10R: INCPT BOI R-115 TO D10.0 AND HOLD.'
    },
    'KBOS': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1500",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURES EXIST RWY 09'
    },
    'KBUR': {
        'thr_red': "835",
        'acc': "855",
        'eo_acc': "1100",
        'EFP': 'RW08: TRK RCL TO D7.6 VNY VOR RT DCT VNY...,RW15: TRK RCL TO CROSS VNY R-105 RT DCT VNY..., TRK RCL TO CROSS VNY R-078 LT DCT VNY...,  HOLD E VNY ON R-078 LT 5NM LEGS ,'
    },
    'KBZN': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURE EXIST ALL RUNWAYS'
    },
    'KDAL': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1500",
        'EFP': 'RW31L: TRK RCL TO 4.0 DME CVE VOR, RT H356. RW31R: TRK RCL TO CROSS CVE VOR R-043, RT H-133'
    },
    'KDCA': {
        'thr_red': "1500",
        'acc': "1500",
        'eo_acc': "1500",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURES EXIST RWY 01'
    },
    'KDEN': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1070",
        'EFP': 'REF 10-7 PAGES'
    },
    'KDFW': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': ' INTERSECTION AND ENTRY POINTS\n.\n35C  FROM ENTRY POINT ER\n.\n35R  FROM ENTRY POINT Q10\n.\n36R FROM ENTRY POINT WR/WQ OR WP\n36RY FROM TAXIWAY A OR B\n.\n36L  FROM ENTRY POINT WR\n'
    },
    'KDRO': {
        'thr_red': "2000",
        'acc': "2000",
        'eo_acc': "2000",
        'EFP': 'RW03: TRK RCL TO DRO D4.0, RT H150. RW21: TRK RCL TO DRO D10.0, LT DCT RSK'
    },
    'KEGE': {
        'thr_red': "3000",
        'acc': "3000",
        'eo_acc': "4550",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURE EXIST ALL RUNWAYS'
    },
    'KELP': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1500",
        'EFP': 'REF 10-7 PAGES'
    },
    'KEUG': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RW16L/R: TRK RCL TO D3.0 EUG VOR LT H-335 TO INTCPT EUG R-345 TRK OUTBOUND'
    },
    'PAFA': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RW02L:TRK RCL TO 8.0 DME FAI VOR, RT H-200. RW20R: TRK RCL TO 10 NM'
    },
    'LIRF': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "SEE 10-7",
        'EFP': 'REF 10-7 PAGES'
    },
    'RJFF': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "3000",
        'EFP': 'RW16: TRK RCL TO 18.0 DME DGC VOR, RT DCT SGE VOR.'
    },
    'KGEG': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RW03: TRK RCL TO GEG D10.0, LT DCT GEG RW08: RT H120 TO D8.0 GEG, RT DCT GEG. RW21/RW26: TRK RCL TO 4000FT, LT DCT GEG'
    },
    'KGJT': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RW11: DT "JETRY" RT "LOMMA" HOLD OVER "LOMMA" RT. RW29: DT "LOMMA"  HOLD OVER "LOMMA" RT'
    },
    'KGTF': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RW21: TRK RCL TO 3.0 DME GTF VOR, RT H-330 TO INCPT GTF R-310 OUTBOUND'
    },
    'MGGT': {
        'thr_red': "1000",
        'acc': "1500",
        'eo_acc': "1500",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURES EXIST ALL RUNWAYS'
    },
    'KGUC': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'REF 10-7 PAGES'
    },
    'PGUM': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RW24L: TRK RCL TO 3.0 DME IAWD (2.1 DME UNZ) RT H-330. RW24R:TRK RCL TO 3.0 DME IGUM (2.1 DME UNZ) RT H-330.'
    },
    'KHDN': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1500",
        'EFP': 'RW10: AT DER RT DCT "REVME" HOLD OVER REVME RT ON R-104. RW28:AT DER DT DCT "REVME" HOLD OVER REVME RT ON R-104.'
    },
    'RJTT': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "3000",
        'EFP': 'RW16L/RW16R: TRK RCL TO 10.0 DME HME V0R LT DIR TO CHIBA (HME R-091/D15.0)... RW34L: TRK RCL TO CROSS HME VOR R-345 (HNDB1) RT HDG 150 DEG TO INTCPT HME R-091 TRK OUTBND... RW34R: TRK RCL TO CROSS HME VOR R-020 (HNDA1) RT HDG 140 DEG TO INTCPT HME R-091 TRK OUTBND, HOLD W OF CHIBA (HME R-091/D15.0) ON HME-091 RT  '
    },
    'PHNL': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RW04L/R: TRK RCL TO 2.2 DME HNL VOR RT H140 (MAX SPEED 175KIAS FIRST TURN). RW08L: TRK RCL TO 3.5 DME HNL VOR RT H170 (MAX SPEED 175KIAS FIRST TURN). RW08R: TRK RCL TO 2.0 DME HNL VOR RT H140 (MAX SPEED 175KIAS FIRST TURN).'
    },
    'RKSI': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1500",
        'EFP': 'RW33L/R: TRK RCL TO 5.0 DME NCN VOR, LT DCT "GE973" HOLD OVER "GE973" LT INBOUND 087 DEG. RW34L/R: TRK RCL TO 7.0 DME WNG VOR, LT DCT "GE973" HOLD OVER "GE973" LT INBOUND 087.'
    },
    'KIAD': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RW30: TRK RCL TO INTCPT AML VOR R-300. TRK OUTBND HOLD NW OF MANNE (AML R-300/D11.0) LEFT TURNS.'
    },
   'KJAC': {
        'thr_red': "1000",
        'acc': "3000",
        'eo_acc': "3000",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURE EXIST ALL RUNWAYS'
    },
    'KJFK': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURES EXIST RWY 31L/R'
    },
    'PAJN': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "3000",
        'EFP': 'REF 10-7 PAGES'
    },
    'RJBB': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "3000",
        'EFP': 'RW24L/24R: TRK RCL TO KIE VOR D3.0 RT H-050 INCPT KIE R-007 TRK OUTBND TO MAYAH (KIE R-007/D10.9) HOLD W OF MAYAH ON YOE R-279 RT...RW06L/R: TRK RCL TO D16.0 SW OF YOE VOR LT H-360 INCPT YOE R-279 TRK OUTBND HOLD W OF MAYAH (YOE R-279/D19.3) ON YOE R-279 RT.'
    },
    'KLAS': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURE EXIST ALL RUNWAYS'
    },
    'KLGA': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1040",
        'EFP': 'RW04: TRK RCL TO D6.0 LGA VOR RT H-080'
    },
    'PHLI': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1500",
        'EFP': 'RW03: TRK RCL TO 4.6 DME LIH VOR, RT H-080. RW21: AT DER LT H-100 (MAX 165 KTS)'
    },
    'SPJC': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RW16L/R: TRK RCL TO 6.0 DME JCL VOR, RT H-170.'
    },
    'LEMD': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1500",
        'EFP': 'REF 10-7 PAGES'
    },
    'KMDW': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1500",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURES EXIST RWY 04R'
    },
    'MMMX': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "2000",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURE EXIST ALL RUNWAYS'
    },
    'KMFR': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1760",
        'EFP': 'RW14: TRK RCL TO 10 DME OED RT DCT OED HOLD SE ON OED R-145 RT RW32: TRK RCL TO 3.0 DME OED DT OED HOLD SE ON OED R-145'
    },
    'KMHT': {
        'thr_red': "3000",
        'acc': "3000",
        'eo_acc': "3000",
        'EFP': 'REF 10-7 PAGES'
    },
    'KMRY': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1500",
        'EFP': 'RW10R: AT 1.0 DME I-MRY, LT DIR SNS VOR'
    },
    'KMSO': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "3000",
        'EFP': 'RW12: TRK RCL TO D2.0 MSO VOR RT (15 DEG BANK) TRK H-190 TO INTCPT MSO R-160 OUTBOUND. RW30: TRK RCL TO D2.0 MSO VOR RT (15 DEG BANK) TRK H-330 TO D4.5 MSO LT (15 DEG BANK) TO MSO, HOLD MSO RT INBND CRS 340 DEG'
    },
    'KMTJ': {
        'thr_red': "2000",
        'acc': "2000",
        'eo_acc': "1850",
        'EFP': 'RW17: TRK RCL TO 2.0 DME MTJ VOR, RT H-340 TO INTCPT MTJ R-297 IF IMC RT DCT MTJ PASSING 8100 MSL, RW35: TRK RCL TO D4.0 MTJ VOR LT TO INTCPT MTJ R-297 IF IMC RT DCT MTJ PASSING 8100 MSL.'
    },
    'LIMC': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1500",
        'EFP': 'REF 10-7 PAGES'
    },
    'MMMZ': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1000",
        'EFP': 'REF 10-7 PAGES'
    },
    'RJAA': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1500",
        'EFP': 'RW34L/R: TRK RCL TO INTCPT NRE VOR R-338 TRK OUTBOUND, HOLD SE OF NRE D15.0/R-338 ON NRE R-338 LEFT TURNS'
    },
    'KOAK': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "3000",
        'EFP': 'RW12: TRK RCL TO 4.0 DME OAK, RT H-150 TO INTCPT OAK R-130 OUTBOUND. RW30: TRK RCL TO 3.0 DME OAK LT H-110 TO INTCPT OAK R-130 OUTBOUND'
    },
    'MMOX': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "2500",
        'EFP': 'REF 10-7 PAGES'
    },
    'KONT': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'REF 10-7 PAGES'
    },
    'KOTH': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'REF 10-7 PAGES'
    },
    'KPDX': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'REF 10-7 PAGES'
    },
    'KPHL': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RWYS WITH S IDENT INCLUDE SHIP OBST.\n    09LS / 09RS / 17S\n 27L VALID WITH OR WITHOUT SHIPS\n\n\n\n\n RWY 27L INTERSECTION DEPARTURES...\n     27LX FROM TXWY S2.\n     27LY FROM TXWY S3\n     27LZ FROM TXWY S4.'
    },
    'KPHX': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURE EXIST RWYS 07L/R 08'
    },
    'MPTO': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1000",
        'EFP': 'R03L/R: TRK RCL TO TUM VOR D4.0, RT H-110 TO D8.0 TUM RT H-180.'
    },    
 
    'NSTU': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RW05: TRK RCL TO 2.5 DME TUT VOR, RT H-090. RW23: AT DER LT H-150 (MAX 185 KIAS). SHIPS UP TO 150 FT CONSIDERED'
    },
    'TJPS': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1500",
        'EFP': 'REF 10-7 PAGES'
    },
    'KPSP': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RW13R: AT DER, LT H-095 TO INCPT TRM R-115 INBND, HOLD OVER TRM, R TURNS. RW31L: TRK RCL TO 3 NM, RT TO INCPT TRM R-115 INBND, HOLD OVER TRM, R TURNS.'
    },
    'MMPR': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1500",
        'EFP': 'REF 10-7 PAGES'
    },
    'KPVU': {
        'thr_red': "3000",
        'acc': "3000",
        'eo_acc': "3000",
        'EFP': 'REF 10-7 PAGES'
    },
    'KRDM': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'RW05: TRK RCL TO 9.0 DME DSD VOR, LT DCT DSD. RW11:TRK RCL TO 9.0 DME DSD VOR, RT DCT DSD. RW23: TRK RCL TO INTCPT DSD R190, RT DCT DSD (R082 INBD). RW29: TRK RCL TO INTCPT DSD R330, LT DCT DSD (R082 INBD)'
    },
    'KRNO': {
        'thr_red': "3000",
        'acc': "3000",
        'eo_acc': "3000",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURE EXISTS ALL RUNWAYS'
    },
    'KSAF': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'REF 10-7 PAGES'
    },
    'KSAN': {
        'thr_red': "1520",
        'acc': "1520",
        'eo_acc': "1520",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURE EXISTS RWY 09'
    },
    'KSBA': {
        'thr_red': "1520",
        'acc': "1520",
        'eo_acc': "1520",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURE EXIST FOR RUNWAY 07'
    },
    'KSBD': {
        'thr_red': "2660",
        'acc': "2660",
        'eo_acc': "2660",
        'EFP': 'RW06: TRK RCL TO 20.0 DME PDZ VOR, RT DCT SB NDB. RW24: TRK DCT SB NDB'
    },
    'KSBP': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1200",
        'EFP': 'REF 10-7 PAGES'
    },
    'KSFO': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "913",
        'EFP': ' SPECIAL ENGINE-FAILURE PROCEDURES\n EXIST 01L/R, 19L/R, 28L/R\n PLEASE BE AWARE OF PERFORMANCE DIFFERENCES BETWEEN \n RUNWAYS 01L AND 10L. ENSURE USE OF THE CORRECT DATA WHEN \n PLANNING AND OPERATING FROM RUNWAYS 01L AND 10L'
    },
    'KSJC': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1500",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURE EXIST FOR RUNWAYS 12L/12R'
    },
    'MMSD': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1000",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURE EXIST FOR RUNWAY 34'
    },
    'MROC': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURE EXISTS RWY 07'
    },
    'MHLM': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1205",
        'EFP': 'SPECIAL ENGINE-FAILURE PROCEDURES EXIST RWY 22.\n  NON-FMS EOSID RW22: TRK RCL TO D2.5 LMS VOR LT H-041 TO D10.0 LMS, TRK H-010 OUTBND'
    },
    'TJSJ': {
        'thr_red': "1000",
        'acc': "1000",
        'eo_acc': "1000",
        'EFP': 'SPECIAL ENGINE-FAILURE PROCEDURES EXIST RWY 26'
    },
    'KSLC': {
        'thr_red': "5030",
        'acc': "5030",
        'eo_acc': "5230",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURE EXIST ALL RUNWAYS'
    },
    'KSNA': {
        'thr_red': "1000",
        'acc': "3000",
        'eo_acc': "1000",
        'EFP': 'SPECIAL ENGINE-FAILURE PROCEDURES EXIST RWY 02L.'
    },
    'TIST': {
        'thr_red': "1500",
        'acc': "1500",
        'eo_acc': "1500",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURE EXIST FOR RWY 10'
    },
    'KSUN': {
        'thr_red': "1500",
        'acc': "1500",
        'eo_acc': "1250",
        'EFP': 'REF 10-7 PAGES'
    },
    'TNCM': {
        'thr_red': "1500",
        'acc': "1500",
        'eo_acc': "1500",
        'EFP': 'SPECIAL ENGINE FAILURE PROCEDURE EXIST RWY 10'
    },
    'KTUS': {
        'thr_red': "3440",
        'acc': "3440",
        'eo_acc': "3700",
        'EFP': 'RW12: TRK RCL TO D3.0 TUS VOR LT H-280 INCPT TUS VOR R-320 AND TRK OUTBND, RW30: TRK RCL TO D4.0 TUS VOR RT H-340 INCPT TUS VOR R-320 AND TRK OUTBND'
    },
    'SEQM': {
        'thr_red': "2000",
        'acc': "2000",
        'eo_acc': "1500",
        'EFP': 'REF 10-7 PAGES'
    },
    'PADQ': {
        'thr_red': "3000",
        'acc': "3000",
        'eo_acc': "3000",
        'EFP': 'ALL RWYS: TRK DCT ODK'
    },
    'CYYC': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1000",
        'EFP': 'REF 10-7 PAGES'
    },
    'CYVR': {
        'thr_red': "1500",
        'acc': "3000",
        'eo_acc': "1000",
        'EFP': 'RWY 08R WEIGHTS APPLY WHEN STARTING\n TAKEOFF FROM TXWY L4/A '
    }
}

   
    # Check if the airport has specific values
    if icao_code in airport_exceptions:
        return airport_exceptions[icao_code]
    
    # Return default calculated values if no exception exists
    default_thr_red_acc = 1000  # Default AFE value
    default_eo_acc = 1000      # Default AFE value
    
    return {
        'thr_red': default_thr_red_acc,
        'acc': default_thr_red_acc,
        'eo_acc': default_eo_acc
    }

