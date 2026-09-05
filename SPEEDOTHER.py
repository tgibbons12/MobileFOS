import re

def get_speed_other(icao_code, weight=None, speed_type=None, oat=None, altitude=None, assumed_temp=None, thrust_rating=26):
    """
    Lookup additional speeds/N1/EPR based on aircraft ICAO code and parameters.
    
    Args:
        icao_code: Aircraft ICAO type code (e.g., 'E75L', 'B738', 'A320', 'MD83')
        weight: Aircraft weight in pounds (for E-Jets and Airbus)
        speed_type: Optional string ('F', 'S', 'GRN DOT') for Airbus types
        oat: Outside Air Temperature in Celsius (for Boeing and MD83)
        altitude: Pressure altitude in feet (for Boeing and MD83)
        assumed_temp: Assumed/flex temperature for reduced thrust (MD83 only)
                     If provided, calculates Takeoff EPR using assumed temp
                     If None, calculates MAX EPR using actual OAT
    
    Returns:
        Dict: For Airbus: {'speed': {'F': x, 'S': y, 'GRN DOT': z}}
              For Boeing: {'name': 'Takeoff Thrust N1', 'n1': value}
              For MD83: {'name': 'MAX EPR' or 'Takeoff EPR', 'epr': value, 'corrections': {...}}
              For others: {'name': speed name, 'speed': value}
        Returns None if no data exists.
    """
    # Boeing 737 N1 data — per thrust rating (OAT × altitude → N1 pack-on)
    # altitudes: [-2000,-1000,0,1000,2000,3000,4000,5000,6000,7000,8000,9000,10000]
    _B737_OAT_TEMPS = [60,55,50,45,40,35,30,25,20,15,10,5,0,-5,-10,-15,-20,-25,-30,-35,-40,-45,-50]
    _B737_ALTITUDES = [-2000,-1000,0,1000,2000,3000,4000,5000,6000,7000,8000,9000,10000]

    # Pack-off adjustment per altitude (add to pack-on N1)
    _B737_PACK_OFF_ADJ = {
        -2000: 0.7, -1000: 0.7, 0: 0.7, 1000: 0.7, 2000: 0.7, 3000: 0.7, 4000: 0.8,
        5000: 0.8, 6000: 0.8, 7000: 0.8, 8000: 0.8, 9000: 0.9, 10000: 1.0
    }

    BOEING_737_N1_BY_RATING = {
        27: {
            60: [99.8,100.4,100.8,102.9,101.0,101.1,101.2,101.3,101.2,100.9,100.8,100.7,100.7],
            55: [100.4,101.0,101.5,101.6,101.7,101.8,101.9,102.1,101.9,101.6,101.3,100.7,100.0],
            50: [101.0,101.6,102.1,102.3,102.4,102.6,102.7,102.8,102.7,102.4,102.1,101.6,101.1],
            45: [101.8,102.4,102.8,102.0,102.1,102.3,102.4,102.5,102.4,102.1,102.8,102.5,102.1],
            40: [102.4,102.1,102.6,102.7,102.8,102.9,102.0,102.2,102.1,102.8,102.5,102.4,102.1],
            35: [102.0,102.7,102.4,102.5,102.6,102.7,102.8,102.9,102.8,102.5,102.2,102.1,102.0],
            30: [102.6,102.8,102.3,102.3,102.4,102.4,102.5,102.5,102.4,102.3,102.0,102.9,102.9],
            25: [101.8,102.1,102.5,102.1,102.7,102.8,102.7,102.7,102.7,102.7,102.6,102.6,102.7],
            20: [101.0,102.3,102.8,102.3,102.9,102.2,102.5,102.8,102.8,102.9,102.8,102.8,102.8],
            15: [100.2,101.5,102.0,102.6,102.2,102.5,102.8,102.1,102.5,102.9,102.1,102.1,102.1],
            10: [99.5,100.8,102.2,102.8,102.4,102.7,102.0,102.4,102.7,102.1,102.5,102.0,102.5],
             5: [99.7,100.0,101.4,102.0,102.6,102.0,102.3,102.6,102.0,102.4,102.8,102.3,102.7],
             0: [99.9,99.2,100.6,101.3,101.9,102.2,102.5,102.9,102.2,102.6,102.0,102.5,102.0],
            -5: [99.0,99.4,99.8,100.5,101.1,101.4,101.7,102.1,102.5,102.9,102.3,102.7,102.2],
           -10: [97.2,99.6,99.0,99.7,100.3,100.6,101.0,101.3,101.7,102.1,102.5,102.0,102.4],
           -15: [96.4,97.7,99.2,99.9,99.5,99.8,100.2,100.6,100.9,101.3,101.7,102.2,102.6],
           -20: [93.6,96.9,99.4,99.0,99.7,99.0,99.4,99.8,100.2,100.6,100.9,101.4,101.8],
           -25: [93.7,96.1,97.6,99.2,99.9,99.2,99.6,99.0,99.4,99.8,100.2,100.6,101.0],
           -30: [92.9,93.2,96.7,97.4,99.0,99.4,99.8,99.2,99.6,99.0,99.3,99.8,100.2],
           -35: [92.0,93.4,93.9,96.5,97.2,97.6,97.9,99.4,99.8,99.1,99.5,99.0,99.4],
           -40: [91.1,92.5,93.0,93.7,96.3,96.7,97.1,97.5,97.9,99.3,99.7,99.1,99.6],
           -45: [91.3,91.6,93.2,93.8,93.5,93.9,96.3,96.7,97.1,97.5,97.9,99.3,99.7],
           -50: [91.4,91.7,92.3,92.9,93.6,93.0,93.4,93.9,96.3,96.6,97.0,97.5,97.9],
        },
        26: {
            60: [94.8,95.4,95.8,95.9,96.0,96.1,96.2,96.3,96.2,95.9,95.8,95.7,95.7],
            55: [95.4,96.0,96.5,96.6,96.7,96.8,96.9,97.1,96.9,96.6,96.3,95.7,95.0],
            50: [96.0,96.6,97.1,97.3,97.4,97.6,97.7,97.8,97.7,97.4,97.1,96.6,96.1],
            45: [96.8,97.4,97.8,98.0,98.1,98.3,98.4,98.5,98.4,98.1,97.8,97.5,97.1],
            40: [97.4,98.1,98.6,98.7,98.8,98.9,99.0,99.2,99.1,98.8,98.5,98.4,98.1],
            35: [98.0,98.7,99.4,99.5,99.6,99.7,99.8,99.9,99.8,99.5,99.2,99.1,99.0],
            30: [97.6,98.8,100.3,100.3,100.4,100.4,100.5,100.5,100.4,100.3,100.0,99.9,99.9],
            25: [96.8,98.1,99.5,100.1,100.7,100.8,100.7,100.7,100.7,100.7,100.6,100.6,100.7],
            20: [96.0,97.3,98.8,99.3,99.9,100.2,100.5,100.8,100.8,100.9,100.8,100.8,100.8],
            15: [95.2,96.5,98.0,98.6,99.2,99.5,99.8,100.1,100.5,100.9,101.1,101.1,101.1],
            10: [94.5,95.8,97.2,97.8,98.4,98.7,99.0,99.4,99.7,100.1,100.5,101.0,101.5],
             5: [93.7,95.0,96.4,97.0,97.6,98.0,98.3,98.6,99.0,99.4,99.8,100.3,100.7],
             0: [92.9,94.2,95.6,96.3,96.9,97.2,97.5,97.9,98.2,98.6,99.0,99.5,100.0],
            -5: [92.0,93.4,94.8,95.5,96.1,96.4,96.7,97.1,97.5,97.9,98.3,98.7,99.2],
           -10: [91.2,92.6,94.0,94.7,95.3,95.6,96.0,96.3,96.7,97.1,97.5,98.0,98.4],
           -15: [90.4,91.7,93.2,93.9,94.5,94.8,95.2,95.6,95.9,96.3,96.7,97.2,97.6],
           -20: [89.6,90.9,92.4,93.0,93.7,94.0,94.4,94.8,95.2,95.6,95.9,96.4,96.8],
           -25: [88.7,90.1,91.6,92.2,92.9,93.2,93.6,94.0,94.4,94.8,95.2,95.6,96.0],
           -30: [87.9,89.2,90.7,91.4,92.0,92.4,92.8,93.2,93.6,94.0,94.3,94.8,95.2],
           -35: [87.0,88.4,89.9,90.5,91.2,91.6,91.9,92.4,92.8,93.1,93.5,94.0,94.4],
           -40: [86.1,87.5,89.0,89.7,90.3,90.7,91.1,91.5,91.9,92.3,92.7,93.1,93.6],
           -45: [85.3,86.6,88.2,88.8,89.5,89.9,90.3,90.7,91.1,91.5,91.9,92.3,92.7],
           -50: [84.4,85.7,87.3,87.9,88.6,89.0,89.4,89.9,90.3,90.6,91.0,91.5,91.9],
        },
    }

    # Keep single-dict alias for non-SFP B738 default (26K)
    BOEING_737_N1_DATA = {
        'name': 'Takeoff Thrust N1',
        'thrust_rating': 26,
        'oat_temps': _B737_OAT_TEMPS,
        'altitudes':  _B737_ALTITUDES,
        'n1_values':  BOEING_737_N1_BY_RATING[26],
    }
    
    # MD83 EPR data structure
    MD83_EPR_DATA = {
        'name': 'Takeoff Thrust EPR',
        'oat_temps': [50, 48, 46, 44, 42, 40, 38, 36, 34, 32, 30, 28, 26, 24, 22, 20, 18, 16, 14, 12, 10, 8, 6, 4, 2, 0, -2, -4, -6, -8, -10, -12, -14, -16],
        'altitudes': [-1000, 0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000],
        'epr_values': {
            50: [1.86, 1.86, 1.86, 1.86, 1.86, 1.86, 1.86, 1.86, 1.86, 1.86],
            48: [1.88, 1.88, 1.88, 1.88, 1.88, 1.88, 1.88, 1.88, 1.88, 1.88],
            46: [1.90, 1.90, 1.90, 1.90, 1.90, 1.90, 1.90, 1.90, 1.90, 1.90],
            44: [1.91, 1.91, 1.91, 1.91, 1.91, 1.91, 1.91, 1.91, 1.91, 1.91],
            42: [1.93, 1.93, 1.93, 1.93, 1.93, 1.93, 1.93, 1.93, 1.93, 1.93],
            40: [1.94, 1.94, 1.94, 1.94, 1.94, 1.94, 1.94, 1.94, 1.94, 1.94],
            38: [1.96, 1.96, 1.96, 1.95, 1.96, 1.96, 1.96, 1.96, 1.96, 1.96],
            36: [1.98, 1.98, 1.98, 1.98, 1.98, 1.98, 1.98, 1.98, 1.98, 1.98],
            34: [1.99, 1.99, 1.99, 1.99, 1.99, 1.99, 1.99, 1.99, 1.99, 1.99],
            32: [1.99, 2.01, 2.01, 2.01, 2.01, 2.01, 2.01, 2.01, 2.01, 2.01],
            30: [1.99, 2.03, 2.03, 2.03, 2.03, 2.03, 2.03, 2.03, 2.03, 2.03],
            28: [1.99, 2.04, 2.04, 2.04, 2.04, 2.04, 2.04, 2.04, 2.04, 2.04],
            26: [1.99, 2.04, 2.06, 2.06, 2.06, 2.06, 2.06, 2.06, 2.06, 2.06],
            24: [1.99, 2.04, 2.06, 2.07, 2.07, 2.07, 2.07, 2.07, 2.07, 2.07],
            22: [1.99, 2.04, 2.06, 2.08, 2.08, 2.08, 2.08, 2.08, 2.08, 2.08],
            20: [1.99, 2.04, 2.06, 2.08, 2.08, 2.08, 2.08, 2.08, 2.08, 2.08],
            18: [1.99, 2.04, 2.06, 2.08, 2.09, 2.09, 2.09, 2.09, 2.09, 2.09],
            16: [1.99, 2.04, 2.06, 2.08, 2.10, 2.10, 2.10, 2.10, 2.10, 2.10],
            14: [1.99, 2.04, 2.06, 2.08, 2.10, 2.10, 2.10, 2.10, 2.10, 2.10],
            12: [1.99, 2.04, 2.06, 2.08, 2.10, 2.10, 2.10, 2.10, 2.10, 2.10],
            10: [1.93, 1.99, 2.02, 2.04, 2.06, 2.06, 2.06, 2.06, 2.06, 2.06],
            8: [1.93, 1.99, 2.02, 2.04, 2.06, 2.06, 2.06, 2.06, 2.06, 2.06],
            6: [1.94, 1.99, 2.02, 2.04, 2.06, 2.06, 2.06, 2.06, 2.06, 2.06],
            4: [1.96, 1.99, 2.02, 2.04, 2.06, 2.06, 2.06, 2.06, 2.06, 2.06],
            2: [1.97, 1.99, 2.02, 2.04, 2.06, 2.06, 2.06, 2.06, 2.06, 2.06],
            0: [1.97, 1.99, 2.02, 2.04, 2.06, 2.06, 2.06, 2.06, 2.06, 2.06],
            -2: [1.97, 2.00, 2.02, 2.04, 2.06, 2.06, 2.06, 2.06, 2.06, 2.06],
            -4: [1.97, 2.02, 2.02, 2.04, 2.06, 2.07, 2.07, 2.07, 2.07, 2.07],
            -6: [1.97, 2.02, 2.03, 2.04, 2.06, 2.08, 2.08, 2.08, 2.08, 2.08],
            -8: [1.97, 2.02, 2.04, 2.04, 2.06, 2.06, 2.06, 2.06, 2.06, 2.06],
            -10: [1.97, 2.02, 2.04, 2.06, 2.06, 2.06, 2.06, 2.06, 2.06, 2.06],
            -12: [1.97, 2.02, 2.04, 2.06, 2.06, 2.06, 2.06, 2.06, 2.06, 2.06],
            -14: [1.97, 2.02, 2.04, 2.06, 2.08, 2.08, 2.06, 2.06, 2.06, 2.06],
            -16: [1.97, 2.02, 2.04, 2.06, 2.08, 2.08, 2.09, 2.09, 2.09, 2.09]
        },
        'corrections': {
            'packs_off': 0.02,
            'engine_anti_ice': 0.0
        }
    }
    
    # Combined data dictionary with ALL aircraft types
    SPEED_OTHER_DATA = {
        # E-Jets
        'E75L': {
            'name': 'VFS',
            'weights': [50000, 52000, 54000, 56000, 58000, 60000, 62000, 64000, 66000, 68000,
                        70000, 72000, 74000, 76000, 78000, 80000, 82000, 84000, 86000],
            'speeds': [157, 160, 163, 166, 169, 172, 175, 178, 181, 183,
                       186, 189, 191, 194, 197, 199, 201, 204, 206]
        },
        'E170': {
            'name': 'VFS',
            'weights': [48000, 50000, 52000, 54000, 56000, 58000, 60000, 62000, 64000, 66000,
                        68000, 70000, 72000, 74000, 76000, 78000, 80000, 82000, 84000, 86000],
            'speeds': [154, 157, 160, 163, 166, 169, 172, 175, 178, 181,
                       183, 186, 189, 191, 194, 197, 199, 201, 204, 206]
        },
        'E190': {
            'name': 'VFS',
            'weights': [66100, 68300, 70500, 72800, 75000, 77200, 79400, 81600, 83800, 86000,
                        88200, 90400, 92600, 94800, 97000, 99200, 101400, 103600],
            'speeds': [161, 164, 167, 169, 172, 174, 177, 179, 182, 184,
                       187, 189, 191, 194, 196, 198, 200, 202]
        },
        'E195': {
            'name': 'VFS',
            'weights': [66100, 68300, 70500, 72800, 75000, 77200, 79400, 81600, 83800, 86000,
                        88200, 90400, 92600, 94800, 97000, 99200, 101400, 103600],
            'speeds': [161, 164, 167, 169, 172, 174, 177, 179, 182, 184,
                       187, 189, 191, 194, 196, 198, 200, 202]
        },
        # ERJ-135/140/145/E45X — weight-based VFS lookup
        # V2+15 and VFS are derived in write_tps_section; only VFS is stored here.
        'E135': {
            'name': 'VFS',
            'weights': [
                30000, 31000, 32000, 33000, 34000, 35000, 36000, 37000, 38000, 39000,
                40000, 41000, 42000, 43000, 44000, 45000, 46000, 47000, 48000, 48500,
                49000, 50000
            ],
            'speeds': [
                141, 142, 145, 147, 150, 152, 154, 156, 158, 160,
                162, 164, 165, 167, 169, 171, 172, 173, 175, 176,
                177, 179
            ]
        },
        'E140': {
            'name': 'VFS',
            'weights': [
                30000, 31000, 32000, 33000, 34000, 35000, 36000, 37000, 38000, 39000,
                40000, 41000, 42000, 43000, 44000, 45000, 46000, 47000, 48000, 48500,
                49000, 50000
            ],
            'speeds': [
                141, 142, 145, 147, 150, 152, 154, 156, 158, 160,
                162, 164, 165, 167, 169, 171, 172, 173, 175, 176,
                177, 179
            ]
        },
        'E145': {
            'name': 'VFS',
            'weights': [
                30000, 31000, 32000, 33000, 34000, 35000, 36000, 37000, 38000, 39000,
                40000, 41000, 42000, 43000, 44000, 45000, 46000, 47000, 48000, 48500,
                49000, 50000
            ],
            'speeds': [
                141, 142, 145, 147, 150, 152, 154, 156, 158, 160,
                162, 164, 165, 167, 169, 171, 172, 173, 175, 176,
                177, 179
            ]
        },
        'E45X': {
            'name': 'VFS',
            'weights': [
                30000, 31000, 32000, 33000, 34000, 35000, 36000, 37000, 38000, 39000,
                40000, 41000, 42000, 43000, 44000, 45000, 46000, 47000, 48000, 48500,
                49000, 50000
            ],
            'speeds': [
                141, 142, 145, 147, 150, 152, 154, 156, 158, 160,
                162, 164, 165, 167, 169, 171, 172, 173, 175, 176,
                177, 179
            ]
        },
        'DH8D': {
            'name': 'VCL',
            'weights': [
                39500, 40000, 41000, 42000, 43000, 44000, 45000, 46000, 47000, 48000,
                49000, 50000, 51000, 52000, 53000, 54000, 55000, 56000, 57000, 58000,
                59000, 60000, 61000, 62000, 63000, 64000
            ],
            'speeds': [
                130, 130, 129, 128, 127, 131, 131, 130, 129, 137,
                137, 136, 135, 135, 136, 136, 137, 137, 138, 138,  # Fixed progression
                139, 139, 140, 140, 141, 141
            ]
        },

        'MD83': {
            'name': 'VsR/VMM',
            'weights': [
                90000, 92000, 94000, 96000, 98000, 100000, 102000, 104000, 106000, 108000,
                110000, 112000, 114000, 116000, 118000, 120000, 122000, 124000, 126000, 128000,
                130000, 132000, 134000, 136000, 138000, 140000, 142000, 144000, 146000, 148000,
                150000, 152000, 154000, 156000, 158000, 160000
            ],
            'speeds': [
                {'VsR': 157, 'VMM': 194},
                {'VsR': 159, 'VMM': 198},  # 92k
                {'VsR': 161, 'VMM': 200},  # 94k
                {'VsR': 163, 'VMM': 202},  # 96k
                {'VsR': 164, 'VMM': 204},  # 98k
                {'VsR': 165, 'VMM': 205},  # 100k
                {'VsR': 167, 'VMM': 207},  # 102k
                {'VsR': 169, 'VMM': 209},  # 104k
                {'VsR': 171, 'VMM': 211},  # 106k
                {'VsR': 172, 'VMM': 213},  # 108k
                {'VsR': 173, 'VMM': 215},  # 110k
                {'VsR': 175, 'VMM': 217},  # 112k
                {'VsR': 177, 'VMM': 219},  # 114k
                {'VsR': 179, 'VMM': 221},  # 116k
                {'VsR': 180, 'VMM': 223},  # 118k
                {'VsR': 181, 'VMM': 225},  # 120k
                {'VsR': 183, 'VMM': 227},  # 122k ← Your example!
                {'VsR': 184, 'VMM': 229},  # 124k
                {'VsR': 186, 'VMM': 231},  # 126k
                {'VsR': 187, 'VMM': 232},  # 128k
                {'VsR': 188, 'VMM': 234},  # 130k
                {'VsR': 190, 'VMM': 236},  # 132k
                {'VsR': 191, 'VMM': 238},  # 134k
                {'VsR': 193, 'VMM': 240},  # 136k
                {'VsR': 194, 'VMM': 241},  # 138k
                {'VsR': 195, 'VMM': 243},  # 140k
                {'VsR': 197, 'VMM': 245},  # 142k
                {'VsR': 198, 'VMM': 247},  # 144k
                {'VsR': 200, 'VMM': 248},  # 146k
                {'VsR': 201, 'VMM': 250},  # 148k
                {'VsR': 202, 'VMM': 251},  # 150k
                {'VsR': 204, 'VMM': 253},  # 152k
                {'VsR': 205, 'VMM': 255},  # 154k
                {'VsR': 207, 'VMM': 257},  # 156k
                {'VsR': 208, 'VMM': 258},  # 158k
                {'VsR': 209, 'VMM': 260},  # 160k
            ]
        },
        'MD83': {
            'name': 'VsR/VMM',
            'weights': [
                90000, 92000, 94000, 96000, 98000, 100000, 102000, 104000, 106000, 108000,
                110000, 112000, 114000, 116000, 118000, 120000, 122000, 124000, 126000, 128000,
                130000, 132000, 134000, 136000, 138000, 140000, 142000, 144000, 146000, 148000,
                150000, 152000, 154000, 156000, 158000, 160000
            ],
            'speeds': [
                {'VsR': 157, 'VMM': 194},
                {'VsR': 159, 'VMM': 198},  # 92k
                {'VsR': 161, 'VMM': 200},  # 94k
                {'VsR': 163, 'VMM': 202},  # 96k
                {'VsR': 164, 'VMM': 204},  # 98k
                {'VsR': 165, 'VMM': 205},  # 100k
                {'VsR': 167, 'VMM': 207},  # 102k
                {'VsR': 169, 'VMM': 209},  # 104k
                {'VsR': 171, 'VMM': 211},  # 106k
                {'VsR': 172, 'VMM': 213},  # 108k
                {'VsR': 173, 'VMM': 215},  # 110k
                {'VsR': 175, 'VMM': 217},  # 112k
                {'VsR': 177, 'VMM': 219},  # 114k
                {'VsR': 179, 'VMM': 221},  # 116k
                {'VsR': 180, 'VMM': 223},  # 118k
                {'VsR': 181, 'VMM': 225},  # 120k
                {'VsR': 183, 'VMM': 227},  # 122k ← Your example!
                {'VsR': 184, 'VMM': 229},  # 124k
                {'VsR': 186, 'VMM': 231},  # 126k
                {'VsR': 187, 'VMM': 232},  # 128k
                {'VsR': 188, 'VMM': 234},  # 130k
                {'VsR': 190, 'VMM': 236},  # 132k
                {'VsR': 191, 'VMM': 238},  # 134k
                {'VsR': 193, 'VMM': 240},  # 136k
                {'VsR': 194, 'VMM': 241},  # 138k
                {'VsR': 195, 'VMM': 243},  # 140k
                {'VsR': 197, 'VMM': 245},  # 142k
                {'VsR': 198, 'VMM': 247},  # 144k
                {'VsR': 200, 'VMM': 248},  # 146k
                {'VsR': 201, 'VMM': 250},  # 148k
                {'VsR': 202, 'VMM': 251},  # 150k
                {'VsR': 204, 'VMM': 253},  # 152k
                {'VsR': 205, 'VMM': 255},  # 154k
                {'VsR': 207, 'VMM': 257},  # 156k
                {'VsR': 208, 'VMM': 258},  # 158k
                {'VsR': 209, 'VMM': 260},  # 160k
            ]
        },

        # Airbus types with F/S/GRN DOT
        'A319': {
            'name': 'F/S/GRN DOT',
            'weights': [100000, 110000, 120000, 130000, 140000, 150000, 160000, 170000],
            'speeds': [
                {'F': 125, 'S': 163, 'GRN DOT': 176},
                {'F': 131, 'S': 171, 'GRN DOT': 185},
                {'F': 137, 'S': 179, 'GRN DOT': 194},
                {'F': 142, 'S': 186, 'GRN DOT': 203},
                {'F': 148, 'S': 193, 'GRN DOT': 212},
                {'F': 153, 'S': 200, 'GRN DOT': 221},
                {'F': 158, 'S': 206, 'GRN DOT': 230},
                {'F': 163, 'S': 213, 'GRN DOT': 239},
            ]
        },
        'A320': {
            'name': 'F/S/GRN DOT',
            'weights': [100000, 110000, 120000, 130000, 140000, 150000, 160000, 170000],
            'speeds': [
                {'F': 125, 'S': 161, 'GRN DOT': 176},
                {'F': 131, 'S': 169, 'GRN DOT': 185},
                {'F': 136, 'S': 177, 'GRN DOT': 194},
                {'F': 142, 'S': 184, 'GRN DOT': 203},
                {'F': 147, 'S': 191, 'GRN DOT': 212},
                {'F': 152, 'S': 198, 'GRN DOT': 221},
                {'F': 157, 'S': 203, 'GRN DOT': 230},
                {'F': 162, 'S': 210, 'GRN DOT': 239},
            ]
        },
        'A321': {
            'name': 'F/S/GRN DOT',
            'weights': [110000, 120000, 130000, 140000, 150000, 160000, 170000, 180000, 190000, 200000, 210000],
            'speeds': [
                {'F': 130, 'S': 165, 'GRN DOT': 185},
                {'F': 133, 'S': 172, 'GRN DOT': 192},
                {'F': 139, 'S': 179, 'GRN DOT': 199},
                {'F': 144, 'S': 186, 'GRN DOT': 205},
                {'F': 149, 'S': 192, 'GRN DOT': 212},
                {'F': 154, 'S': 198, 'GRN DOT': 219},
                {'F': 159, 'S': 204, 'GRN DOT': 226},
                {'F': 163, 'S': 210, 'GRN DOT': 233},
                {'F': 168, 'S': 216, 'GRN DOT': 240},
                {'F': 172, 'S': 222, 'GRN DOT': 246},
                {'F': 176, 'S': 227, 'GRN DOT': 254},
            ]
        },
        'A21N': {
            'name': 'F/S/GRN DOT',
            'weights': [110000, 120000, 130000, 140000, 150000, 160000, 170000, 180000, 190000, 200000, 210000],
            'speeds': [
                {'F': 130, 'S': 165, 'GRN DOT': 185},
                {'F': 133, 'S': 172, 'GRN DOT': 192},
                {'F': 139, 'S': 179, 'GRN DOT': 199},
                {'F': 144, 'S': 186, 'GRN DOT': 205},
                {'F': 149, 'S': 192, 'GRN DOT': 212},
                {'F': 154, 'S': 198, 'GRN DOT': 219},
                {'F': 159, 'S': 204, 'GRN DOT': 226},
                {'F': 163, 'S': 210, 'GRN DOT': 233},
                {'F': 168, 'S': 216, 'GRN DOT': 240},
                {'F': 172, 'S': 222, 'GRN DOT': 246},
                {'F': 176, 'S': 227, 'GRN DOT': 254},
            ]
        },
        # Boeing 737 variants
        'B738': BOEING_737_N1_DATA,
        'B38M': BOEING_737_N1_DATA
    }

    # Normalise type-code variants onto the keys this table actually uses.
    # One airframe has several codes: the E175 appears as E175 (base_type),
    # E75L / E75S / E75W (short/long/winglet ICAO variants) and E17X. The
    # table is keyed E75L, so a straight lookup missed and VFS came back
    # empty. Same story across the E-Jet family.
    _ALIASES = {
        # E175 family — long, short and winglet variants are one aircraft.
        'E175': 'E75L', 'E75L': 'E75L', 'E75S': 'E75L', 'E75W': 'E75L', 'E17X': 'E75L',
        # E170 family.
        'E170': 'E170', 'E70L': 'E170', 'E70S': 'E170', 'E70W': 'E170',
        # E190 family (incl. E2).
        'E190': 'E190', 'E90L': 'E190', 'E90S': 'E190', 'E290': 'E190', 'E19X': 'E190',
        # E195 family (incl. E2).
        'E195': 'E195', 'E95L': 'E195', 'E95S': 'E195', 'E295': 'E195',
    }
    if icao_code:
        _key = str(icao_code).upper().replace('-', '').replace(' ', '')
        if _key in _ALIASES:
            icao_code = _ALIASES[_key]
        elif _key not in SPEED_OTHER_DATA:
            # Pattern fallback so a variant nobody has enumerated yet still
            # resolves instead of silently returning None. Order matters:
            # 175 must be tested before the looser 17x rule.
            import re as _re
            if _re.match(r'^E(175|75[A-Z]|17[A-Z])$', _key):   icao_code = 'E75L'
            elif _re.match(r'^E(170|70[A-Z])$', _key):          icao_code = 'E170'
            elif _re.match(r'^E(190|90[A-Z]|290|29[A-Z])$', _key): icao_code = 'E190'
            elif _re.match(r'^E(195|95[A-Z]|295)$', _key):      icao_code = 'E195'

    if icao_code not in SPEED_OTHER_DATA:
        return None

    data = SPEED_OTHER_DATA[icao_code]

    # Handle Boeing N1 data (requires OAT and altitude)
    if icao_code in ['B738', 'B38M']:
        if oat is None or altitude is None:
            return None

        try:
            oat = float(oat)
            altitude = float(altitude)
            thrust_rating = int(thrust_rating)
        except (TypeError, ValueError):
            return None

        # Select N1 table by thrust rating; fall back to 26K if rating not found
        n1_by_rating = BOEING_737_N1_BY_RATING
        if thrust_rating not in n1_by_rating:
            thrust_rating = 26
        n1_values = n1_by_rating[thrust_rating]

        oat_temps = _B737_OAT_TEMPS
        altitudes  = _B737_ALTITUDES
        
        # Find OAT indices for interpolation
        if oat >= oat_temps[0]:
            oat_idx1 = 0
            oat_idx2 = 0
            oat_factor = 0.0
        elif oat <= oat_temps[-1]:
            oat_idx1 = len(oat_temps) - 1
            oat_idx2 = len(oat_temps) - 1
            oat_factor = 0.0
        else:
            oat_idx1 = 0
            oat_idx2 = 1
            oat_factor = 0.0
            
            for i in range(len(oat_temps) - 1):
                if oat_temps[i] >= oat >= oat_temps[i + 1]:
                    oat_idx1 = i
                    oat_idx2 = i + 1
                    oat_factor = (oat_temps[i] - oat) / (oat_temps[i] - oat_temps[i + 1])
                    break
        
        # Find altitude indices for interpolation
        if altitude <= altitudes[0]:
            alt_idx1 = 0
            alt_idx2 = 0
            alt_factor = 0.0
        elif altitude >= altitudes[-1]:
            alt_idx1 = len(altitudes) - 1
            alt_idx2 = len(altitudes) - 1
            alt_factor = 0.0
        else:
            alt_idx1 = 0
            alt_idx2 = 1
            alt_factor = 0.0
            
            for i in range(len(altitudes) - 1):
                if altitudes[i] <= altitude <= altitudes[i + 1]:
                    alt_idx1 = i
                    alt_idx2 = i + 1
                    alt_factor = (altitude - altitudes[i]) / (altitudes[i + 1] - altitudes[i])
                    break
        
        # Get the four corner N1 values
        oat_key1 = oat_temps[oat_idx1]
        oat_key2 = oat_temps[oat_idx2]
        
        n1_11 = n1_values[oat_key1][alt_idx1]
        n1_12 = n1_values[oat_key1][alt_idx2]
        n1_21 = n1_values[oat_key2][alt_idx1]
        n1_22 = n1_values[oat_key2][alt_idx2]
        
        # Bilinear interpolation
        n1_1 = n1_11 + (n1_12 - n1_11) * alt_factor
        n1_2 = n1_21 + (n1_22 - n1_21) * alt_factor
        n1 = n1_1 + (n1_2 - n1_1) * oat_factor
        
        # Pack-off adjustment from takeoff_adj table (interpolate between altitude points)
        alt_clamped = max(_B737_ALTITUDES[0], min(_B737_ALTITUDES[-1], altitude))
        adj_keys = sorted(_B737_PACK_OFF_ADJ.keys())
        pack_off_adj = _B737_PACK_OFF_ADJ.get(int(alt_clamped))
        if pack_off_adj is None:
            # Interpolate
            for ai in range(len(adj_keys) - 1):
                if adj_keys[ai] <= alt_clamped <= adj_keys[ai + 1]:
                    frac = (alt_clamped - adj_keys[ai]) / (adj_keys[ai+1] - adj_keys[ai])
                    pack_off_adj = _B737_PACK_OFF_ADJ[adj_keys[ai]] + frac * (
                        _B737_PACK_OFF_ADJ[adj_keys[ai+1]] - _B737_PACK_OFF_ADJ[adj_keys[ai]])
                    break
            else:
                pack_off_adj = 0.8

        return {
            'name': 'Takeoff Thrust N1',
            'n1': round(n1, 1),
            'n1_pack_off': round(n1 + pack_off_adj, 1),
            'thrust_rating': thrust_rating,
        }
    
    # Handle MD83 EPR data (requires OAT and altitude)
    if icao_code == 'MD83' and oat is not None and altitude is not None:
        try:
            oat = float(oat)
            altitude = float(altitude)
            # Use assumed_temp if provided (for Takeoff EPR), otherwise use actual OAT (for MAX EPR)
            temp_for_lookup = float(assumed_temp) if assumed_temp is not None else oat
        except (TypeError, ValueError):
            return None
        
        oat_temps = MD83_EPR_DATA['oat_temps']
        altitudes = MD83_EPR_DATA['altitudes']
        epr_values = MD83_EPR_DATA['epr_values']
        
        # Find OAT indices for interpolation
        if temp_for_lookup >= oat_temps[0]:
            oat_idx1 = 0
            oat_idx2 = 0
            oat_factor = 0.0
        elif temp_for_lookup <= oat_temps[-1]:
            oat_idx1 = len(oat_temps) - 1
            oat_idx2 = len(oat_temps) - 1
            oat_factor = 0.0
        else:
            oat_idx1 = 0
            oat_idx2 = 1
            oat_factor = 0.0
            
            for i in range(len(oat_temps) - 1):
                if oat_temps[i] >= temp_for_lookup >= oat_temps[i + 1]:
                    oat_idx1 = i
                    oat_idx2 = i + 1
                    oat_factor = (oat_temps[i] - temp_for_lookup) / (oat_temps[i] - oat_temps[i + 1])
                    break
        
        # Find altitude indices for interpolation
        if altitude <= altitudes[0]:
            alt_idx1 = 0
            alt_idx2 = 0
            alt_factor = 0.0
        elif altitude >= altitudes[-1]:
            alt_idx1 = len(altitudes) - 1
            alt_idx2 = len(altitudes) - 1
            alt_factor = 0.0
        else:
            alt_idx1 = 0
            alt_idx2 = 1
            alt_factor = 0.0
            
            for i in range(len(altitudes) - 1):
                if altitudes[i] <= altitude <= altitudes[i + 1]:
                    alt_idx1 = i
                    alt_idx2 = i + 1
                    alt_factor = (altitude - altitudes[i]) / (altitudes[i + 1] - altitudes[i])
                    break
        
        # Get the four corner EPR values
        oat_key1 = oat_temps[oat_idx1]
        oat_key2 = oat_temps[oat_idx2]
        
        epr_11 = epr_values[oat_key1][alt_idx1]
        epr_12 = epr_values[oat_key1][alt_idx2]
        epr_21 = epr_values[oat_key2][alt_idx1]
        epr_22 = epr_values[oat_key2][alt_idx2]
        
        # Bilinear interpolation
        epr_1 = epr_11 + (epr_12 - epr_11) * alt_factor
        epr_2 = epr_21 + (epr_22 - epr_21) * alt_factor
        epr = epr_1 + (epr_2 - epr_1) * oat_factor
        
        # Determine if this is MAX EPR or Takeoff EPR
        epr_type = 'Takeoff EPR' if assumed_temp is not None else 'MAX EPR'
        
        result = {
            'name': epr_type,
            'epr': round(epr, 2),
            'altitude': altitude,
            'corrections': MD83_EPR_DATA['corrections']
        }
        
        # Add temperature info based on mode
        if assumed_temp is not None:
            result['assumed_temp'] = assumed_temp
            result['actual_oat'] = oat
        else:
            result['oat'] = oat
        
        return result
    
    # Handle weight-based data (E-Jets and Airbus)
    if weight is None:
        return None
    
    weights = data['weights']
    speeds = data['speeds']

    try:
        weight = float(weight)
    except (TypeError, ValueError):
        return None

    # Find the index range for interpolation
    if weight <= weights[0]:
        # Below minimum weight - use first entry
        speed_entry = speeds[0]
        if isinstance(speed_entry, dict):
            return {'speed': speed_entry}
        return {'name': data['name'], 'speed': speed_entry}
    
    elif weight >= weights[-1]:
        # Above maximum weight - use last entry
        speed_entry = speeds[-1]
        if isinstance(speed_entry, dict):
            return {'speed': speed_entry}
        return {'name': data['name'], 'speed': speed_entry}
    
    else:
        # Interpolate between two weight points
        idx1 = 0
        idx2 = 1
        for i in range(len(weights)-1):
            if weights[i] <= weight <= weights[i+1]:
                idx1 = i
                idx2 = i + 1
                break
        
        # Calculate interpolation factor
        weight_factor = (weight - weights[idx1]) / (weights[idx2] - weights[idx1])
        
        # Check if this is Airbus (dict-based) or E-Jets (numeric)
        if isinstance(speeds[idx1], dict):
            # Airbus: interpolate each speed component
            interpolated_speeds = {}
            for key in speeds[idx1].keys():
                val1 = speeds[idx1][key]
                val2 = speeds[idx2][key]
                interpolated_val = val1 + (val2 - val1) * weight_factor
                interpolated_speeds[key] = round(interpolated_val)
            return {'speed': interpolated_speeds}
        else:
            # E-Jets: interpolate single numeric value
            speed1 = speeds[idx1]
            speed2 = speeds[idx2]
            interpolated_speed = speed1 + (speed2 - speed1) * weight_factor
            return {'name': data['name'], 'speed': round(interpolated_speed)}


def get_reduced_thrust_n1(icao_code, thrust_rating, assumed_temp, altitude):
    """
    Lookup reduced thrust N1 based on aircraft, thrust rating, assumed temp, and altitude.
    
    Args:
        icao_code: Aircraft ICAO type code ('B738', 'B38M')
        thrust_rating: Engine thrust rating (26, 24, 22, 20, 18)
        assumed_temp: Assumed temperature in Celsius
        altitude: Pressure altitude in feet
    
    Returns:
        Dict: {'name': 'Reduced Takeoff Thrust N1', 'n1': value, 'thrust_rating': rating, 'assumed_temp': temp}
        Returns None if no data exists.
    """
    # B738 and B38M share the same reduced thrust data
# B738 and B38M ADJUSTED N1 values (Base N1 - Adjustment applied)
 # These are reduced thrust values with temperature adjustments already applied
# Assumes typical OAT conditions (around 15–30°C) with assumed temps set higher

    BOEING_737_REDUCED_THRUST_27K = {
        'assumed_temps': [75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 25, 20, 15, 10],
        'altitudes': [-1000, 0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000],
        'n1_values': {
            75: [95.1, 95.4, 95.9, 96.4, 97.2, 98.0, 98.9, 99.0, 99.1, 99.1, 98.9, 98.6],
            70: [95.7, 95.9, 96.0, 96.1, 96.4, 97.3, 98.2, 98.2, 98.4, 98.3, 98.2, 98.1],
            65: [94.3, 94.5, 94.8, 94.9, 95.1, 95.4, 95.5, 95.8, 95.7, 95.9, 95.6, 95.4],
            60: [94.7, 95.1, 95.3, 95.6, 95.8, 96.1, 96.2, 95.9, 95.5, 95.1, 94.8, 94.7],
            55: [96.5, 97.1, 97.3, 97.5, 97.8, 98.0, 98.4, 97.9, 97.4, 97.0, 96.2, 95.3],
            50: [96.9, 97.5, 97.9, 98.1, 98.6, 98.8, 99.0, 98.8, 98.3, 97.7, 97.1, 96.5],
            45: [99.0, 99.4, 99.8, 100.0, 100.4, 100.7, 100.9, 100.7, 100.2, 99.7, 99.3, 98.8],
            40: [99.6, 100.2, 100.4, 100.6, 100.9, 101.1, 101.5, 101.4, 100.9, 100.4, 100.2, 99.8],
            35: [100.0, 101.0, 101.2, 101.4, 101.7, 101.9, 102.2, 102.0, 101.5, 101.0, 100.8, 100.7],
            30: [101.2, 103.1, 103.2, 103.4, 103.4, 103.7, 103.8, 103.6, 103.5, 102.9, 102.8, 102.9],
            25: [100.5, 102.2, 102.9, 103.6, 103.5, 103.3, 103.4, 103.4, 103.5, 103.4, 103.4, 103.7],
            20: [99.7, 101.6, 102.1, 102.7, 102.7, 102.7, 102.6, 102.7, 102.9, 102.8, 102.9, 103.0],
            15: [98.9, 100.8, 101.5, 102.1, 102.1, 102.1, 102.0, 102.1, 102.2, 102.2, 102.2, 102.3],
            10: [99.6, 101.4, 102.0, 102.7, 102.7, 102.7, 102.8, 102.7, 102.7, 102.8, 102.9, 103.0]
        }
    }

    BOEING_737_REDUCED_THRUST_26K = {
        'assumed_temps': [75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 25, 20, 15, 10],
        'altitudes': [-1000, 0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000],
        'n1_values': {
            75: [88.3, 88.7, 88.8, 88.9, 89.0, 89.1, 89.2, 89.1, 88.8, 88.7, 88.6, 88.6],
            70: [88.9, 89.4, 89.5, 89.6, 89.7, 89.8, 90.0, 89.8, 89.5, 89.2, 88.6, 87.9],
            65: [89.7, 90.2, 90.4, 90.5, 90.7, 90.8, 90.9, 90.8, 90.5, 90.2, 89.7, 89.2],
            60: [90.5, 90.9, 91.1, 91.2, 91.4, 91.5, 91.6, 91.5, 91.2, 90.9, 90.6, 90.2],
            55: [92.2, 92.7, 92.8, 92.9, 93.0, 93.1, 93.3, 93.2, 92.9, 92.6, 92.5, 92.2],
            50: [92.8, 93.5, 93.6, 93.7, 93.8, 93.9, 94.0, 93.9, 93.6, 93.3, 93.2, 93.1],
            45: [94.2, 95.7, 95.7, 95.8, 95.8, 95.9, 95.9, 95.8, 95.7, 95.4, 95.3, 95.3],
            40: [93.5, 94.9, 95.5, 96.1, 96.2, 96.1, 96.1, 96.1, 96.1, 96.0, 96.0, 96.1],
            35: [94.3, 95.8, 96.3, 96.9, 97.2, 97.5, 97.8, 97.8, 97.9, 97.8, 97.8, 97.8],
            30: [93.5, 95.0, 95.6, 96.2, 96.5, 96.8, 97.1, 97.5, 97.9, 98.1, 98.1, 98.1],
            25: [94.3, 95.7, 96.3, 96.9, 97.2, 97.5, 97.9, 98.2, 98.6, 99.0, 99.5, 100.0],
            20: [93.5, 94.9, 95.5, 96.1, 96.4, 96.7, 97.1, 97.5, 97.9, 98.3, 98.8, 99.2],
            15: [94.2, 95.6, 96.2, 96.8, 97.1, 97.4, 97.8, 98.2, 98.6, 99.0, 99.5, 100.0],
            10: [93.4, 94.8, 95.4, 96.0, 96.3, 96.6, 97.0, 97.4, 97.8, 98.3, 98.7, 99.2]
        }
    }

    BOEING_737_REDUCED_THRUST_24K = {
        'assumed_temps': [75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 25, 20, 15, 10],
        'altitudes': [-1000, 0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000],
        'n1_values': {
            75: [93.4, 93.7, 94.2, 94.7, 95.4, 96.1, 96.9, 97.3, 97.6, 97.8, 97.8, 97.7],
            70: [85.5, 85.8, 85.8, 85.8, 88.4, 89.1, 89.9, 90.3, 90.6, 90.8, 90.8, 90.8],
            65: [86.3, 86.6, 86.7, 86.7, 86.8, 86.9, 87.0, 87.6, 87.8, 88.1, 88.0, 88.0],
            60: [86.9, 87.3, 87.4, 87.5, 87.6, 87.7, 87.8, 87.7, 87.4, 87.3, 87.2, 87.2],
            55: [88.5, 89.0, 89.1, 89.2, 89.3, 89.4, 89.6, 89.4, 89.1, 88.8, 88.2, 87.5],
            50: [89.1, 89.6, 89.8, 89.9, 90.1, 90.2, 90.3, 90.2, 89.9, 89.6, 89.1, 88.6],
            45: [91.2, 91.6, 91.8, 91.9, 92.1, 92.2, 92.3, 92.2, 91.9, 91.6, 91.3, 90.9],
            40: [92.0, 92.4, 92.5, 92.6, 92.7, 92.8, 93.0, 92.9, 92.6, 92.3, 92.2, 91.9],
            35: [94.1, 94.8, 94.9, 95.0, 95.1, 95.2, 95.3, 95.2, 94.9, 94.6, 94.5, 94.4],
            30: [94.2, 95.7, 95.7, 95.8, 95.8, 95.9, 95.9, 95.8, 95.7, 95.4, 95.3, 95.3],
            25: [96.6, 96.6, 97.2, 97.8, 97.9, 97.8, 97.8, 97.8, 97.8, 97.7, 97.7, 97.8],
            20: [94.4, 95.9, 96.4, 97.0, 97.2, 97.5, 97.9, 97.9, 98.0, 97.9, 97.9, 97.9],
            15: [95.0, 96.5, 97.1, 97.7, 98.0, 98.3, 98.6, 99.0, 99.4, 99.6, 99.6, 99.6],
            10: [94.3, 95.7, 96.3, 96.9, 97.2, 97.5, 97.9, 98.2, 98.6, 99.0, 99.5, 100.0]
        }
    }

    
    BOEING_737_REDUCED_THRUST_22K = {
        'assumed_temps': [75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 25, 20, 15, 10],
        'altitudes': [-1000, 0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000],
        'n1_values': {
            75: [88.3, 88.6, 89.1, 89.6, 90.2, 90.8, 91.5, 92.2, 92.7, 93.1, 93.3, 93.4],
            70: [83.2, 83.6, 83.4, 83.4, 84.0, 84.5, 85.2, 85.7, 86.1, 86.6, 86.7, 86.8],
            65: [85.2, 85.6, 85.4, 85.4, 85.4, 85.3, 85.4, 86.1, 86.6, 87.0, 87.1, 87.3],
            60: [86.0, 86.4, 86.3, 86.3, 86.3, 86.2, 86.3, 86.4, 86.2, 86.4, 86.5, 86.6],
            55: [88.1, 88.5, 88.5, 88.5, 88.4, 88.4, 88.4, 88.4, 88.4, 88.2, 87.8, 87.3],
            50: [86.6, 87.5, 87.5, 87.5, 87.4, 87.4, 87.4, 87.4, 87.3, 87.3, 86.9, 86.6],
            45: [87.2, 87.6, 87.6, 87.6, 87.6, 87.5, 87.5, 87.5, 87.4, 87.3, 87.1, 86.8],
            40: [88.0, 88.4, 88.4, 88.4, 88.3, 88.3, 88.2, 88.2, 88.1, 88.1, 88.0, 87.8],
            35: [90.2, 90.6, 90.6, 90.6, 90.5, 90.5, 90.4, 90.4, 90.3, 90.2, 90.2, 90.1],
            30: [90.4, 91.5, 91.4, 91.4, 91.4, 91.3, 91.2, 91.2, 91.1, 91.1, 91.0, 91.0],
            25: [91.2, 92.3, 92.8, 93.3, 93.6, 93.6, 93.5, 93.5, 93.4, 93.3, 93.3, 93.2],
            20: [90.4, 91.5, 92.0, 92.6, 93.2, 93.8, 94.5, 94.4, 94.4, 94.3, 94.2, 94.1],
            15: [91.2, 92.3, 92.8, 93.4, 94.0, 94.6, 95.3, 96.0, 96.7, 97.1, 97.1, 97.0],
            10: [90.5, 91.5, 92.1, 92.6, 93.2, 93.8, 94.5, 95.2, 96.0, 96.7, 97.6, 98.5]
        }
    }
    
    BOEING_737_REDUCED_THRUST_20K = {
        'assumed_temps': [75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 25, 20, 15, 10],
        'altitudes': [-1000, 0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000],
        'n1_values': {
            75: [85.7, 86.0, 86.7, 87.4, 88.2, 88.9, 89.5, 90.1, 90.2, 90.2, 90.6, 91.1],
            70: [81.2, 81.6, 81.7, 81.7, 82.1, 82.9, 83.5, 84.0, 84.1, 84.2, 84.6, 85.0],
            65: [83.3, 83.7, 83.9, 83.9, 84.1, 84.2, 84.2, 84.7, 84.8, 84.8, 85.3, 85.7],
            60: [84.2, 84.6, 84.7, 84.8, 85.0, 85.1, 85.1, 85.0, 84.5, 84.2, 84.6, 85.1],
            55: [84.6, 85.0, 85.2, 85.3, 85.4, 85.5, 85.5, 85.5, 85.0, 84.5, 84.3, 84.1],
            50: [85.3, 85.9, 86.0, 86.1, 86.2, 86.4, 86.3, 86.3, 85.9, 85.4, 85.2, 85.1],
            45: [85.3, 85.9, 86.0, 86.1, 86.2, 86.4, 86.3, 86.3, 85.9, 85.5, 85.4, 85.2],
            40: [85.7, 86.2, 86.3, 86.4, 86.5, 86.6, 86.5, 86.5, 86.2, 85.8, 85.7, 85.6],
            35: [87.9, 88.4, 88.5, 88.6, 88.6, 88.7, 88.7, 88.6, 88.3, 87.9, 87.9, 87.8],
            30: [88.0, 89.2, 89.3, 89.4, 89.4, 89.5, 89.4, 89.3, 89.1, 88.8, 88.7, 88.6],
            25: [88.8, 90.0, 90.6, 91.3, 91.7, 91.8, 91.7, 91.7, 91.3, 90.9, 90.9, 90.9],
            20: [88.0, 89.2, 89.9, 90.5, 91.2, 91.9, 92.5, 92.5, 92.2, 91.8, 91.7, 91.6],
            15: [88.9, 90.1, 90.7, 91.3, 92.1, 92.8, 93.3, 93.8, 94.4, 94.6, 94.4, 94.0],
            10: [88.1, 89.3, 89.9, 90.6, 91.3, 92.0, 92.5, 93.0, 93.6, 94.2, 94.9, 95.6]
        }
    }
    
    BOEING_737_REDUCED_THRUST_18K = {
        'assumed_temps': [75, 70, 65, 60, 55, 50, 45, 40, 35, 30, 25, 20, 15, 10],
        'altitudes': [-1000, 0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000],
        'n1_values': {
            75: [81.4, 81.5, 84.0, 85.8, 87.2, 88.8, 89.7, 90.6, 90.4, 90.1, 89.8, 89.4],
            70: [77.1, 77.2, 78.9, 80.1, 81.2, 82.8, 83.7, 84.5, 84.3, 84.1, 83.8, 83.4],
            65: [79.3, 79.6, 81.1, 82.3, 83.1, 84.1, 84.4, 85.2, 85.0, 84.8, 84.5, 84.0],
            60: [80.3, 80.6, 82.0, 83.2, 84.0, 85.0, 85.2, 85.4, 84.7, 84.1, 83.8, 83.4],
            55: [81.2, 81.7, 82.9, 84.0, 84.9, 85.9, 86.0, 86.2, 85.5, 84.7, 83.8, 82.8],
            50: [82.2, 82.7, 83.8, 84.8, 85.7, 86.7, 86.8, 86.9, 86.2, 85.5, 84.6, 83.6],
            45: [82.6, 83.1, 84.1, 85.1, 86.1, 87.1, 87.1, 87.1, 86.5, 85.8, 84.9, 84.0],
            40: [83.6, 84.0, 85.1, 86.0, 87.0, 87.9, 87.8, 87.8, 87.2, 86.6, 85.7, 84.8],
            35: [84.4, 84.9, 86.0, 86.9, 87.8, 88.8, 88.7, 88.6, 87.9, 87.3, 86.4, 85.5],
            30: [84.7, 85.9, 86.8, 87.9, 88.7, 89.7, 89.5, 89.4, 88.8, 88.1, 87.2, 86.3],
            25: [85.5, 86.6, 87.6, 88.7, 89.6, 90.7, 91.1, 91.6, 91.1, 90.4, 89.5, 88.6],
            20: [84.8, 85.9, 86.9, 88.0, 88.8, 89.9, 90.3, 90.8, 91.4, 91.2, 90.3, 89.4],
            15: [85.7, 86.8, 87.8, 88.8, 89.7, 90.7, 91.1, 91.6, 92.2, 92.7, 92.7, 91.9],
            10: [84.9, 86.0, 87.0, 88.1, 88.9, 90.0, 90.4, 90.8, 91.4, 91.9, 92.2, 92.8]
        }
    }
    
    REDUCED_THRUST_DATA = {
        'B738': {
            'name': 'Reduced Takeoff Thrust N1',
            'thrust_ratings': {
                27: BOEING_737_REDUCED_THRUST_27K,
                26: BOEING_737_REDUCED_THRUST_26K,
                24: BOEING_737_REDUCED_THRUST_24K,
                22: BOEING_737_REDUCED_THRUST_22K,
                20: BOEING_737_REDUCED_THRUST_20K
            }
        },
        'B38M': {
            'name': 'Reduced Takeoff Thrust N1',
            'thrust_ratings': {
                27: BOEING_737_REDUCED_THRUST_27K,
                26: BOEING_737_REDUCED_THRUST_26K,
                24: BOEING_737_REDUCED_THRUST_24K,
                22: BOEING_737_REDUCED_THRUST_22K,
                20: BOEING_737_REDUCED_THRUST_20K
            },
            'labels': {
                27: 'TO',     # full thrust
                26: 'TO-1',   # derate 1
                24: 'TO-2',   # derate 2
                22: 'TO-3',   # derate 3
                20: 'TO-4'    # derate 4
            }
        }
    }
    
    if icao_code not in REDUCED_THRUST_DATA:
        return None
    
    aircraft_data = REDUCED_THRUST_DATA[icao_code]
    
    if thrust_rating not in aircraft_data['thrust_ratings']:
        return None
    
    rating_data = aircraft_data['thrust_ratings'][thrust_rating]
    
    try:
        assumed_temp = float(assumed_temp)
        altitude = float(altitude)
    except (TypeError, ValueError):
        return None
    
    assumed_temps = rating_data['assumed_temps']
    altitudes = rating_data['altitudes']
    n1_values = rating_data['n1_values']
    
    # Find assumed temp indices for interpolation
    if assumed_temp >= assumed_temps[0]:
        temp_idx1 = 0
        temp_idx2 = 0
        temp_factor = 0.0
    elif assumed_temp <= assumed_temps[-1]:
        temp_idx1 = len(assumed_temps) - 1
        temp_idx2 = len(assumed_temps) - 1
        temp_factor = 0.0
    else:
        # Initialize defaults
        temp_idx1 = 0
        temp_idx2 = 1
        temp_factor = 0.0
        
        for i in range(len(assumed_temps) - 1):
            if assumed_temps[i] >= assumed_temp >= assumed_temps[i + 1]:
                temp_idx1 = i
                temp_idx2 = i + 1
                temp_factor = (assumed_temps[i] - assumed_temp) / (assumed_temps[i] - assumed_temps[i + 1])
                break
    
    # Find altitude indices for interpolation
    if altitude <= altitudes[0]:
        alt_idx1 = 0
        alt_idx2 = 0
        alt_factor = 0.0
    elif altitude >= altitudes[-1]:
        alt_idx1 = len(altitudes) - 1
        alt_idx2 = len(altitudes) - 1
        alt_factor = 0.0
    else:
        # Initialize defaults
        alt_idx1 = 0
        alt_idx2 = 1
        alt_factor = 0.0
        
        for i in range(len(altitudes) - 1):
            if altitudes[i] <= altitude <= altitudes[i + 1]:
                alt_idx1 = i
                alt_idx2 = i + 1
                alt_factor = (altitude - altitudes[i]) / (altitudes[i + 1] - altitudes[i])
                break
    
    # Get the four corner N1 values
    temp_key1 = assumed_temps[temp_idx1]
    temp_key2 = assumed_temps[temp_idx2]
    
    n1_11 = n1_values[temp_key1][alt_idx1]
    n1_12 = n1_values[temp_key1][alt_idx2]
    n1_21 = n1_values[temp_key2][alt_idx1]
    n1_22 = n1_values[temp_key2][alt_idx2]
    
    # Bilinear interpolation
    n1_1 = n1_11 + (n1_12 - n1_11) * alt_factor
    n1_2 = n1_21 + (n1_22 - n1_21) * alt_factor
    n1 = n1_1 + (n1_2 - n1_1) * temp_factor
    
    return {
        'name': aircraft_data['name'], 
        'n1': round(n1, 1), 
        'thrust_rating': thrust_rating, 
        'assumed_temp': assumed_temp
    }



# ===========================================================================
# Airbus takeoff thrust — engine-keyed, measured from the fleet's own aircraft
# ===========================================================================
# Keyed on (ICAO type, engine family) rather than type alone: one type
# genuinely has several grids (the A320-211 ODM publishes separate CFM56-5A1
# and 5A3 tables), and which parameter is set follows the ENGINE, not the
# airframe — IAE V2500 is EPR-rated, CFM56/LEAP/PW1100G are N1-rated.
#
# TOGA and FLEX are stored SEPARATELY and are not interchangeable. Measured
# on the A321 V2500 at 9933 ft, TOGA sits +0.008 above the flex curve at
# 17C but -0.025 below it at 24C — they cross, so neither can stand in for
# the other. (They are also never needed at the same temperature: the
# dispatch script forces TOGA when the assumed temp is within a few degrees
# of OAT, so the two are used in disjoint regimes.)
#
# Each grid is stored as one temperature curve PER ALTITUDE rather than a
# rectangular table, because real capture is ragged — different altitudes
# get different temperatures sampled. Lookup interpolates along the
# temperature curve within each bracketing altitude first, then blends the
# two results across altitude.
#
# Gap guards matter more than usual here. Published CFM data shows thrust
# vs pressure altitude is a HUMP whose peak moves with temperature (sea
# level at 30C, 4000 ft at 22C, still climbing at 8000 ft at 10C), so
# interpolating across a wide unmeasured altitude span silently invents a
# straight line through a curve — worth up to 1.3 %N1 in the published
# tables. Rather than do that, a lookup whose bracketing samples are
# further apart than max_alt_gap / max_temp_gap returns nothing at all and
# the TPS simply omits the block.

# Matched against the real engine designation (SimBrief aircraft/engines,
# e.g. "IAE V2533-A5"), never against the free-text comments field — an
# earlier pass matched comments and silently failed, because the comment
# for an IAE A321 reads "A321 -A5 SHARKLET" with no engine name in it.
# CFM/LEAP/PW are tested before IAE so that CFM56-5A5, which also ends in
# A5, can never be misread as a V2500.
# Resolved to the exact dash number, because the ODMs publish a separate
# N1 table per variant and they really do differ — the A320's 5A1 and 5A3
# tables disagree in 258 of their 260 cells. A variant with no grid of its
# own therefore resolves to a family that isn't in AIRBUS_TAKEOFF_THRUST
# and shows nothing, rather than borrowing a sibling's numbers. Specific
# patterns must stay ahead of the generic ones.
_ENGINE_FAMILIES = [
    (r'LEAP',             'LEAP-1A',   'N1'),
    (r'PW11\d{2}|PW1100', 'PW1100G',   'N1'),
    (r'CFM56-?5A1',       'CFM56-5A1', 'N1'),
    (r'CFM56-?5A3',       'CFM56-5A3', 'N1'),
    (r'CFM56-?5A5',       'CFM56-5A5', 'N1'),
    (r'CFM56-?5B3',       'CFM56-5B3', 'N1'),
    (r'CFM56-?5A',        'CFM56-5A',  'N1'),
    (r'CFM56-?5B',        'CFM56-5B',  'N1'),
    (r'V2\d{3}|IAE',      'V2500',     'EPR'),
    # 757/767. Order matters: 535E4-B also matches 535E4, and CF6-80C2B8F
    # also matches CF6-80, so the longer name has to be tried first.
    (r'RB211-?535E4-?B',  'RB211-535E4-B', 'EPR'),
    (r'RB211-?535E4',     'RB211-535E4',   'EPR'),
    (r'RB211-?524H',      'RB211-524H',    'EPR'),
    (r'PW2037',           'PW2037',        'EPR'),
    (r'PW2040',           'PW2040',        'EPR'),
    (r'PW4056',           'PW4056',        'EPR'),
    (r'PW4060',           'PW4060',        'EPR'),
    (r'CF6-?80C2B8F',     'CF6-80C2B8F',   'N1'),
    (r'CF6-?80C2B6F',     'CF6-80C2B6F',   'N1'),
    (r'CF6-?80A',         'CF6-80A',       'N1'),
]


def engine_family(engine):
    """('V2500', 'EPR') from a SimBrief engine string like 'V2533-A5', or
    (None, None) when unrecognised. Deliberately returns nothing rather
    than guessing a default: showing an N1 for an EPR-rated engine, or
    either from the wrong grid, is worse than showing no value."""
    s = (engine or '').upper().replace(' ', '')
    if not s:
        return None, None
    for pattern, family, param in _ENGINE_FAMILIES:
        if re.search(pattern, s):
            return family, param
    return None, None


# WHICH AIRCRAFT EACH GRID IS KEYED TO
# -----------------------------------
# The EPR grids are keyed to ToLiss, deliberately, and are NOT corrected
# toward the real AA sheets. The number this app prints is a crosscheck
# against the ECAM the pilot is actually looking at, so agreeing with the
# aircraft in front of them beats agreeing with a document. Measured on
# the three near-sea-level AA A321 -A5 sheets, ToLiss reads a uniform
# 0.010 low (AA prints 1.61 at OAT 18, 24 and 26; this grid gives 1.600).
# That offset is expected, not a defect — do not "fix" it.
#
# The N1 grids are keyed to the real AA TPS instead, by decision — the
# sim is not the reference for these and is not to be measured against
# them. The Delta ODM tables are kept because they ARE that data at
# higher resolution: they reproduce all fifteen values across the five
# real AA A321 -5B3/P sheets to 0.04 N1, which is rounding. Five sheets
# alone would cover four temperatures at two altitudes; the ODM covers
# -54..54C over -1000..8000 ft and agrees with them everywhere they
# overlap, so it is strictly the better form of the same source.
#
# The consequence to be aware of: EPR follows the sim, N1 follows the
# real aircraft. If a ToLiss ECAM ever disagrees with a printed N1, that
# is expected under this policy and is not a defect to chase.
#
# One real difference is also unresolved: AA's MAX EPR rises with
# altitude (1.61 near sea level, 1.64 at PA 3653) where ToLiss falls
# (1.600 at PA 0, 1.589 at PA 5020). The published CFM N1 table rises
# too, so ToLiss is the outlier. Keyed to ToLiss, this grid follows
# ToLiss.

# Flat-rate reference temperature (Tref) for the IAE V2533-A5, read off 112
# takeoff-analysis pages of the A321-231 ODM (issue 10 SEP 13). Tref is the
# temperature above which thrust starts falling off — the knee in every
# curve in AIRBUS_TAKEOFF_THRUST below.
#
# Not used by the lookup. It is kept for two reasons. First, it is an
# independent check that held: the ODM puts Tref at 30C for every field
# between 10 and 82 ft, and the KMIA flex sweep — measured in the sim,
# from an unrelated source — goes flat through 25/30 and breaks at 31.
# Second, it says where new samples are worth taking. Interpolation error
# is worst across the knee, so a temperature curve that straddles its
# altitude's Tref without a sample near it is the one to fill in next.
#
# The relationship flattens with altitude (~205 ft per degree near the
# surface, ~655 ft per degree by 3200 ft), so this must not be
# extrapolated linearly — doing so puts Tref at 14C for 5020 ft, whereas
# the measured knee at KFNL is 24C.
IAE_V2533_TREF = {   # mean field elevation (ft): Tref (C)
    36: 30, 201: 29, 531: 28, 754: 27, 1551: 23,
    1788: 22, 2154: 21, 2492: 20, 3266: 19,
}

AIRBUS_TAKEOFF_THRUST = {
    ('A321', 'V2500'): {
        'param': 'EPR',
        # +0.01, measured: the delta in all five real AA A321 -A5 TPS
        # examples, identical across PA -27..3653, OAT 14..26, sharklet and
        # non-sharklet, wet and dry.
        'bleed_adjust': {'apu_on': 0.01},
        'max_alt_gap': 5100,
        'max_temp_gap': 12,
        # Within this distance of a measured altitude, use that column
        # directly instead of interpolating — being 500 ft from a sampled
        # field is not the same as sitting in the unmeasured middle.
        'alt_snap': 1500,
        # TOGA: {pressure altitude: {OAT: EPR}}
        'toga': {
            0: {                       # KMIA, QNH 1013 — break at 30C, which is
                15: 1.600, 20: 1.600,  # exactly the Tref the ODM publishes for
                25: 1.600, 30: 1.600,  # sea level (see IAE_V2533_TREF above)
                35: 1.554, 40: 1.520, 45: 1.471,
            },
            # An earlier session read 37 -> 1.537 and 40 -> 1.511 here. The
            # sweep above supersedes both: it is self-consistent, and it
            # resolves the plateau that two samples 22C apart could not
            # show. They agree to within 0.009 where they overlap.
            5020: {                    # KFNL, QNH 1013 — break ~24C
                1: 1.589, 5: 1.589,    # KGJT; plateau confirmed unbroken 1..15
                15: 1.589,             # flat-rated below the ~24C break
                24: 1.553,
                31: 1.494,             # KGJT, PA 4858 — see below
                38: 1.440,
            },
            # The 31C entry was read at KGJT, 162 ft below KFNL. Folded into
            # this column rather than given its own: 162 ft is worth 0.0004
            # EPR here, and a second column that close would leave bracket
            # gaps too small to mean anything. It is also the best check
            # this grid has -- a different airport on a different day
            # reproduced the KFNL column to +0.001 at 14C and -0.002 at 31C.
            9933: {                    # KLXV, QNH 29.92 — break ~12C
                0: 1.602, 12: 1.578, 17: 1.543,
                20: 1.510, 24: 1.467, 29: 1.432,
            },
        },
        # FLEX: {pressure altitude: {assumed temp: EPR}}
        #
        # Stored WITHOUT an actual-OAT axis, because the real AA TPS prints
        # it that way and the real examples bear it out: flex 37 reads 1.56
        # at OAT 18 and at OAT 26 alike. Checked against four real AA A321
        # -A5 flex rows, this table lands within 0.015 on the three at or
        # near sea level. The fourth, at PA 3653, is 0.075 low — that is
        # the altitude hump, not an OAT effect, and it is the single
        # biggest known gap in this grid (see the missing 5020 row below).
        #
        # A sweep taken at KFNL (PA 5020, actual OAT 38) was DELIBERATELY
        # NOT LOADED. Every value in it — flex 25/30/35/40 → 1.415/1.390/
        # 1.366/1.343 — falls BELOW both the PA 0 and PA 9933 readings at
        # the same flex temperature, which no altitude relationship can
        # produce. It was something other than a flex EPR (a fixed derate,
        # most likely, since most of its range sat below the actual OAT).
        # Discarding it leaves a hole at mid altitude; filling that hole
        # with data known to be wrong would have been worse.
        'flex': {
            0: {                       # swept at actual OAT 15
                84: 1.331, 72: 1.331, 71: 1.333, 70: 1.338, 65: 1.365,
                60: 1.394, 58: 1.406, 55: 1.424, 53: 1.436, 50: 1.455,
                48: 1.469, 45: 1.489, 43: 1.503, 40: 1.523, 37: 1.545,
                35: 1.560, 32: 1.583, 30: 1.598, 25: 1.600,
            },
            5020: {                    # KFNL, swept at actual OAT 15; clamps at 60
                60: 1.391, 55: 1.409, 50: 1.432, 45: 1.456,
                40: 1.481, 35: 1.507, 30: 1.534, 25: 1.561,
            },
            9933: {                    # swept at actual OAT 12; clamps at 41
                41: 1.395, 40: 1.402, 35: 1.429, 30: 1.457,
                25: 1.486, 20: 1.516, 15: 1.547,
            },
        },
    },
    # Seeded entirely from five real AA A321 -5B3/P TPS sheets (TPA, PDX,
    # BOS, PHL, YYC), not from the sim. Note N1 RISES with OAT — on a
    # flat-rated engine the shaft has to spin faster to hold the same
    # thrust as the air thins — which is the opposite of the EPR grids
    # above and an easy thing to "correct" the wrong way.
    ('A321', 'CFM56-5B3'): {
        'param': 'N1',
        # +0.9, exactly, on all five real AA A321 -5B3/P sheets, and the ODM
        # independently gives Packs Off as +0.9 against the same baseline.
        # Those are the same case: with the APU feeding the packs, the
        # engines stop bleeding for them.
        'bleed_adjust': {'apu_on': 0.9},
        # TAKE OFF %N1, Delta A321 ODM p4-6 (25 MAR 16), parsed from the
        # PDF rather than typed. Baseline is the ODM's NORMAL BLEED /
        # PACKS ON / ANTI-ICE OFF column. Blank cells in the source are
        # labelled OUTSIDE ENVIRONMENTAL ENVELOPE, which is why the
        # extrapolation bounds below matter: clamping across them would
        # answer an uncertified corner with a certified-looking number.
        'max_alt_gap': 1000,
        'max_temp_gap': 4,
        'alt_snap': None,
        'max_temp_extrap': 4,
        'max_alt_extrap': 1000,
        'toga': {
            -1000: {54: 94.8, 50: 95.7, 46: 96.6, 42: 97.4, 38: 98.0, 34: 98.9, 30: 99.1,
                  26: 98.5, 22: 97.8, 18: 97.2, 14: 96.6, 10: 96.0, 6: 95.3, 2: 94.7,
                  -2: 94.0, -6: 93.4, -10: 92.7, -14: 92.1, -18: 91.4, -22: 90.7, -26: 90.0,
                  -30: 89.3, -34: 88.6, -38: 88.0, -42: 87.3, -46: 86.5, -50: 85.8, -54: 85.1,},
            0: {54: 95.3, 50: 96.2, 46: 97.1, 42: 98.1, 38: 98.8, 34: 99.6, 30: 100.9,
                  26: 100.3, 22: 99.7, 18: 99.1, 14: 98.5, 10: 97.8, 6: 97.2, 2: 96.5,
                  -2: 95.8, -6: 95.2, -10: 94.5, -14: 93.9, -18: 93.2, -22: 92.5, -26: 91.8,
                  -30: 91.1, -34: 90.4, -38: 89.7, -42: 89.0, -46: 88.3, -50: 87.6, -54: 86.8,},
            1000: {50: 96.2, 46: 97.1, 42: 98.0, 38: 98.8, 34: 99.8, 30: 101.0, 26: 101.4,
                  22: 100.7, 18: 100.1, 14: 99.5, 10: 98.8, 6: 98.2, 2: 97.5, -2: 96.9,
                  -6: 96.2, -10: 95.5, -14: 94.9, -18: 94.2, -22: 93.5, -26: 92.8, -30: 92.1,
                  -34: 91.4, -38: 90.7, -42: 90.0, -46: 89.3, -50: 88.5, -54: 87.8,},
            2000: {50: 96.2, 46: 97.1, 42: 97.9, 38: 98.7, 34: 99.8, 30: 101.1, 26: 102.4,
                  22: 101.7, 18: 101.1, 14: 100.5, 10: 99.8, 6: 99.2, 2: 98.5, -2: 97.8,
                  -6: 97.2, -10: 96.5, -14: 95.9, -18: 95.2, -22: 94.5, -26: 93.8, -30: 93.1,
                  -34: 92.4, -38: 91.6, -42: 90.9, -46: 90.2, -50: 89.5, -54: 88.7,},
            3000: {42: 97.9, 38: 98.7, 34: 99.8, 30: 101.0, 26: 102.0, 22: 102.1, 18: 101.4,
                  14: 100.8, 10: 100.1, 6: 99.5, 2: 98.8, -2: 98.1, -6: 97.5, -10: 96.8,
                  -14: 96.2, -18: 95.5, -22: 94.8, -26: 94.1, -30: 93.4, -34: 92.7, -38: 92.0,
                  -42: 91.3, -46: 90.5, -50: 89.8, -54: 89.0,},
            4000: {42: 97.8, 38: 98.6, 34: 99.8, 30: 101.0, 26: 102.0, 22: 102.4, 18: 101.7,
                  14: 101.1, 10: 100.4, 6: 99.8, 2: 99.1, -2: 98.5, -6: 97.8, -10: 97.1,
                  -14: 96.5, -18: 95.8, -22: 95.1, -26: 94.4, -30: 93.7, -34: 93.0, -38: 92.2,
                  -42: 91.5, -46: 90.8, -50: 90.0, -54: 89.3,},
            5000: {42: 97.7, 38: 98.5, 34: 99.5, 30: 101.1, 26: 102.1, 22: 102.4, 18: 102.0,
                  14: 101.4, 10: 100.7, 6: 100.1, 2: 99.4, -2: 98.7, -6: 98.1, -10: 97.4,
                  -14: 96.8, -18: 96.1, -22: 95.3, -26: 94.6, -30: 93.9, -34: 93.2, -38: 92.5,
                  -42: 91.8, -46: 91.1, -50: 90.3, -54: 89.6,},
            6000: {42: 97.4, 38: 98.2, 34: 99.0, 30: 100.4, 26: 101.5, 22: 102.2, 18: 102.3,
                  14: 101.7, 10: 101.0, 6: 100.4, 2: 99.7, -2: 99.1, -6: 98.4, -10: 97.7,
                  -14: 97.1, -18: 96.4, -22: 95.7, -26: 95.0, -30: 94.2, -34: 93.5, -38: 92.8,
                  -42: 92.1, -46: 91.4, -50: 90.6, -54: 89.9,},
            7000: {38: 97.9, 34: 98.7, 30: 99.7, 26: 101.0, 22: 101.8, 18: 102.2, 14: 102.0,
                  10: 101.4, 6: 100.7, 2: 100.0, -2: 99.4, -6: 98.7, -10: 98.0, -14: 97.4,
                  -18: 96.7, -22: 96.0, -26: 95.3, -30: 94.5, -34: 93.8, -38: 93.1, -42: 92.4,
                  -46: 91.7, -50: 90.9, -54: 90.2,},
            8000: {38: 97.7, 34: 98.4, 30: 99.1, 26: 100.3, 22: 101.3, 18: 101.9, 14: 102.3,
                  10: 101.6, 6: 101.0, 2: 100.3, -2: 99.7, -6: 99.0, -10: 98.3, -14: 97.7,
                  -18: 97.0, -22: 96.3, -26: 95.5, -30: 94.8, -34: 94.1, -38: 93.4, -42: 92.7,
                  -46: 92.0, -50: 91.2, -54: 90.5,},
        },
        # Flex stays modelled rather than tabulated: the ODM publishes MAX
        # takeoff N1 only, and reading that table at the assumed
        # temperature does not reproduce the real AA flex rows (off by 3.7
        # N1 at TPA). See the OAT correction in get_takeoff_thrust.
        'flex_oat_ref': 20,
        'flex_oat_coeff': 0.152,
        'flex': {
            0:    {45: 93.35, 46: 93.01, 49: 91.94, 51: 91.20},
            3438: {30: 99.39},
        },
    },
    ('A320', 'CFM56-5A1'): {
        'param': 'N1',
        # The ODM prints Packs Off as -0.7 here, where the A319's own ODM
        # prints +0.7 and the A321's +0.9 — Delta's manuals genuinely
        # disagree in sign across types (both pages read visually, this is
        # not a parse artifact). Only the A321 value has independent
        # confirmation, from the AA sheets. Recorded as published.
        'bleed_adjust': {'apu_on': -0.7},
        # TAKE OFF %N1, Delta A320 ODM p4-6 (25 MAR 16), parsed from the
        # PDF rather than typed. Baseline is the ODM's NORMAL BLEED /
        # PACKS ON / ANTI-ICE OFF column. Blank cells in the source are
        # labelled OUTSIDE ENVIRONMENTAL ENVELOPE, which is why the
        # extrapolation bounds below matter: clamping across them would
        # answer an uncertified corner with a certified-looking number.
        'max_alt_gap': 1000,
        'max_temp_gap': 4,
        'alt_snap': None,
        'max_temp_extrap': 4,
        'max_alt_extrap': 1000,
        'toga': {
            -1000: {54: 92.2, 50: 92.8, 46: 93.4, 42: 93.9, 38: 94.5, 34: 95.1, 30: 95.1,
                  26: 94.5, 22: 93.9, 18: 93.3, 14: 92.6, 10: 92.0, 6: 91.4, 2: 90.8,
                  -2: 90.2, -6: 89.5, -10: 88.9, -14: 88.2, -18: 87.6, -22: 86.9, -26: 86.3,
                  -30: 85.6, -34: 85.0, -38: 84.3, -42: 83.6, -46: 82.9, -50: 82.2, -54: 81.5,},
            0: {54: 93.1, 50: 93.7, 46: 94.3, 42: 94.9, 38: 95.4, 34: 95.9, 30: 96.3,
                  26: 95.7, 22: 95.1, 18: 94.5, 14: 93.9, 10: 93.3, 6: 92.6, 2: 92.0,
                  -2: 91.4, -6: 90.7, -10: 90.1, -14: 89.4, -18: 88.8, -22: 88.1, -26: 87.4,
                  -30: 86.8, -34: 86.1, -38: 85.4, -42: 84.7, -46: 84.0, -50: 83.3, -54: 82.6,},
            1000: {50: 93.7, 46: 94.3, 42: 94.8, 38: 95.3, 34: 95.8, 30: 96.3, 26: 96.2,
                  22: 95.6, 18: 95.0, 14: 94.4, 10: 93.8, 6: 93.1, 2: 92.5, -2: 91.9,
                  -6: 91.2, -10: 90.6, -14: 89.9, -18: 89.3, -22: 88.6, -26: 87.9, -30: 87.2,
                  -34: 86.6, -38: 85.9, -42: 85.2, -46: 84.5, -50: 83.8, -54: 83.1,},
            2000: {50: 93.6, 46: 94.2, 42: 94.7, 38: 95.3, 34: 95.8, 30: 96.3, 26: 96.8,
                  22: 96.1, 18: 95.5, 14: 94.9, 10: 94.3, 6: 93.6, 2: 93.0, -2: 92.4,
                  -6: 91.7, -10: 91.1, -14: 90.4, -18: 89.7, -22: 89.1, -26: 88.4, -30: 87.7,
                  -34: 87.0, -38: 86.3, -42: 85.6, -46: 84.9, -50: 84.2, -54: 83.5,},
            3000: {46: 94.1, 42: 94.7, 38: 95.2, 34: 95.7, 30: 96.2, 26: 96.7, 22: 96.6,
                  18: 96.0, 14: 95.4, 10: 94.7, 6: 94.1, 2: 93.5, -2: 92.8, -6: 92.2,
                  -10: 91.5, -14: 90.9, -18: 90.2, -22: 89.5, -26: 88.8, -30: 88.2, -34: 87.5,
                  -38: 86.8, -42: 86.1, -46: 85.4, -50: 84.6, -54: 83.9,},
            4000: {46: 94.1, 42: 94.6, 38: 95.1, 34: 95.6, 30: 96.1, 26: 96.6, 22: 97.1,
                  18: 96.5, 14: 95.9, 10: 95.2, 6: 94.6, 2: 94.0, -2: 93.3, -6: 92.7,
                  -10: 92.0, -14: 91.3, -18: 90.7, -22: 90.0, -26: 89.3, -30: 88.6, -34: 87.9,
                  -38: 87.2, -42: 86.5, -46: 85.8, -50: 85.1, -54: 84.4,},
            5000: {42: 94.6, 38: 95.1, 34: 95.6, 30: 96.0, 26: 96.6, 22: 97.1, 18: 97.0,
                  14: 96.4, 10: 95.7, 6: 95.1, 2: 94.5, -2: 93.8, -6: 93.1, -10: 92.5,
                  -14: 91.8, -18: 91.1, -22: 90.5, -26: 89.8, -30: 89.1, -34: 88.4, -38: 87.7,
                  -42: 87.0, -46: 86.3, -50: 85.5, -54: 84.8,},
            6000: {42: 94.6, 38: 95.0, 34: 95.5, 30: 96.0, 26: 96.5, 22: 97.0, 18: 97.4,
                  14: 96.8, 10: 96.2, 6: 95.5, 2: 94.9, -2: 94.2, -6: 93.6, -10: 92.9,
                  -14: 92.2, -18: 91.5, -22: 90.9, -26: 90.2, -30: 89.5, -34: 88.8, -38: 88.1,
                  -42: 87.4, -46: 86.6, -50: 85.9, -54: 85.2,},
            7000: {38: 95.0, 34: 95.5, 30: 95.9, 26: 96.4, 22: 96.9, 18: 97.3, 14: 97.2,
                  10: 96.6, 6: 95.9, 2: 95.3, -2: 94.6, -6: 93.9, -10: 93.3, -14: 92.6,
                  -18: 91.9, -22: 91.2, -26: 90.5, -30: 89.9, -34: 89.1, -38: 88.4, -42: 87.7,
                  -46: 87.0, -50: 86.3, -54: 85.5,},
            8000: {38: 94.7, 34: 95.2, 30: 95.6, 26: 96.1, 22: 96.5, 18: 97.0, 14: 97.4,
                  10: 96.8, 6: 96.1, 2: 95.5, -2: 94.8, -6: 94.1, -10: 93.5, -14: 92.8,
                  -18: 92.1, -22: 91.4, -26: 90.7, -30: 90.0, -34: 89.3, -38: 88.6, -42: 87.9,
                  -46: 87.2, -50: 86.5, -54: 85.7,},
        },
    },
    ('A320', 'CFM56-5A3'): {
        'param': 'N1',
        # The ODM prints Packs Off as -0.7 here, where the A319's own ODM
        # prints +0.7 and the A321's +0.9 — Delta's manuals genuinely
        # disagree in sign across types (both pages read visually, this is
        # not a parse artifact). Only the A321 value has independent
        # confirmation, from the AA sheets. Recorded as published.
        'bleed_adjust': {'apu_on': -0.7},
        # TAKE OFF %N1, Delta A320 ODM p4-7 (25 MAR 16), parsed from the
        # PDF rather than typed. Baseline is the ODM's NORMAL BLEED /
        # PACKS ON / ANTI-ICE OFF column. Blank cells in the source are
        # labelled OUTSIDE ENVIRONMENTAL ENVELOPE, which is why the
        # extrapolation bounds below matter: clamping across them would
        # answer an uncertified corner with a certified-looking number.
        'max_alt_gap': 1000,
        'max_temp_gap': 4,
        'alt_snap': None,
        'max_temp_extrap': 4,
        'max_alt_extrap': 1000,
        'toga': {
            -1000: {54: 92.4, 50: 93.2, 46: 94.0, 42: 94.8, 38: 95.1, 34: 95.4, 30: 95.2,
                  26: 94.6, 22: 94.0, 18: 93.4, 14: 92.8, 10: 92.2, 6: 91.6, 2: 91.0,
                  -2: 90.3, -6: 89.7, -10: 89.1, -14: 88.4, -18: 87.8, -22: 87.1, -26: 86.5,
                  -30: 85.8, -34: 85.1, -38: 84.4, -42: 83.8, -46: 83.1, -50: 82.4, -54: 81.7,},
            0: {54: 92.8, 50: 93.6, 46: 94.4, 42: 95.1, 38: 95.6, 34: 95.9, 30: 96.2,
                  26: 95.6, 22: 95.0, 18: 94.4, 14: 93.8, 10: 93.2, 6: 92.5, 2: 91.9,
                  -2: 91.3, -6: 90.6, -10: 90.0, -14: 89.3, -18: 88.7, -22: 88.0, -26: 87.4,
                  -30: 86.7, -34: 86.0, -38: 85.3, -42: 84.6, -46: 83.9, -50: 83.2, -54: 82.5,},
            1000: {50: 94.2, 46: 94.9, 42: 95.6, 38: 96.2, 34: 96.5, 30: 96.8, 26: 96.7,
                  22: 96.1, 18: 95.4, 14: 94.8, 10: 94.2, 6: 93.6, 2: 92.9, -2: 92.3,
                  -6: 91.6, -10: 91.0, -14: 90.3, -18: 89.7, -22: 89.0, -26: 88.3, -30: 87.6,
                  -34: 86.9, -38: 86.3, -42: 85.6, -46: 84.9, -50: 84.1, -54: 83.4,},
            2000: {50: 94.9, 46: 95.5, 42: 96.1, 38: 96.6, 34: 97.1, 30: 97.4, 26: 97.7,
                  22: 97.1, 18: 96.4, 14: 95.8, 10: 95.2, 6: 94.5, 2: 93.9, -2: 93.2,
                  -6: 92.6, -10: 91.9, -14: 91.3, -18: 90.6, -22: 89.9, -26: 89.2, -30: 88.6,
                  -34: 87.9, -38: 87.2, -42: 86.5, -46: 85.7, -50: 85.0, -54: 84.3,},
            3000: {46: 95.7, 42: 96.2, 38: 96.7, 34: 97.2, 30: 97.5, 26: 97.8, 22: 97.6,
                  18: 97.0, 14: 96.4, 10: 95.7, 6: 95.1, 2: 94.4, -2: 93.8, -6: 93.1,
                  -10: 92.5, -14: 91.8, -18: 91.1, -22: 90.5, -26: 89.8, -30: 89.1, -34: 88.4,
                  -38: 87.7, -42: 87.0, -46: 86.2, -50: 85.5, -54: 84.8,},
            4000: {46: 96.1, 42: 96.6, 38: 97.1, 34: 97.6, 30: 98.0, 26: 98.4, 22: 98.7,
                  18: 98.1, 14: 97.5, 10: 96.8, 6: 96.2, 2: 95.5, -2: 94.8, -6: 94.2,
                  -10: 93.5, -14: 92.8, -18: 92.2, -22: 91.5, -26: 90.8, -30: 90.1, -34: 89.4,
                  -38: 88.7, -42: 87.9, -46: 87.2, -50: 86.5, -54: 85.7,},
            5000: {42: 96.9, 38: 97.4, 34: 97.9, 30: 98.3, 26: 98.8, 22: 99.3, 18: 99.2,
                  14: 98.5, 10: 97.9, 6: 97.2, 2: 96.5, -2: 95.9, -6: 95.2, -10: 94.5,
                  -14: 93.8, -18: 93.2, -22: 92.5, -26: 91.8, -30: 91.1, -34: 90.3, -38: 89.6,
                  -42: 88.9, -46: 88.2, -50: 87.4, -54: 86.7,},
            6000: {42: 97.1, 38: 97.6, 34: 98.1, 30: 98.6, 26: 99.1, 22: 99.7, 18: 100.2,
                  14: 99.5, 10: 98.9, 6: 98.2, 2: 97.5, -2: 96.9, -6: 96.2, -10: 95.5,
                  -14: 94.8, -18: 94.1, -22: 93.4, -26: 92.7, -30: 92.0, -34: 91.3, -38: 90.5,
                  -42: 89.8, -46: 89.1, -50: 88.3, -54: 87.6,},
            7000: {38: 97.6, 34: 98.1, 30: 98.6, 26: 99.1, 22: 99.6, 18: 100.2, 14: 100.1,
                  10: 99.4, 6: 98.8, 2: 98.1, -2: 97.4, -6: 96.7, -10: 96.1, -14: 95.4,
                  -18: 94.7, -22: 94.0, -26: 93.2, -30: 92.5, -34: 91.8, -38: 91.1, -42: 90.3,
                  -46: 89.6, -50: 88.8, -54: 88.1,},
            8000: {38: 97.2, 34: 97.7, 30: 98.2, 26: 98.7, 22: 99.3, 18: 99.9, 14: 100.4,
                  10: 99.8, 6: 99.1, 2: 98.4, -2: 97.8, -6: 97.1, -10: 96.4, -14: 95.7,
                  -18: 95.0, -22: 94.3, -26: 93.6, -30: 92.8, -34: 92.1, -38: 91.4, -42: 90.6,
                  -46: 89.9, -50: 89.1, -54: 88.4,},
        },
    },
    ('A319', 'CFM56-5A5'): {
        'param': 'N1',
        # +0.7 as published (verified against the page image; the minus
        # signs on the anti-ice rows below it survive, so the absence of
        # one here is real). See the A320 entries — the sign is not
        # consistent across Delta's own manuals for this engine family.
        'bleed_adjust': {'apu_on': 0.7},
        # TAKE OFF %N1, Delta A319 ODM p4-7 (25 MAR 16), parsed from the
        # PDF rather than typed. Baseline is the ODM's NORMAL BLEED /
        # PACKS ON / ANTI-ICE OFF column. Blank cells in the source are
        # labelled OUTSIDE ENVIRONMENTAL ENVELOPE, which is why the
        # extrapolation bounds below matter: clamping across them would
        # answer an uncertified corner with a certified-looking number.
        'max_alt_gap': 1000,
        'max_temp_gap': 4,
        'alt_snap': None,
        'max_temp_extrap': 4,
        'max_alt_extrap': 1000,
        # Two cells are absent from the PA 3000 column that the manual does
        # print: OAT -50 and -54, where it gives 94.4 and 94.8. Those are
        # ~13 N1 above both horizontal neighbours and are verbatim copies
        # of the 50C and 46C entries at the top of the same column — a
        # typesetting error, not data. Dropping them costs the -54C corner
        # at that one altitude, which now returns nothing.
        'toga': {
            -1000: {54: 90.7, 50: 91.3, 46: 92.0, 42: 92.6, 38: 92.3, 34: 91.8, 30: 91.2,
                  26: 90.6, 22: 90.0, 18: 89.5, 14: 88.9, 10: 88.3, 6: 87.7, 2: 87.1,
                  -2: 86.5, -6: 85.9, -10: 85.3, -14: 84.7, -18: 84.0, -22: 83.4, -26: 82.8,
                  -30: 82.1, -34: 81.5, -38: 80.8, -42: 80.2, -46: 79.5, -50: 78.9, -54: 78.2,},
            0: {54: 91.3, 50: 92.0, 46: 92.6, 42: 93.2, 38: 93.5, 34: 93.0, 30: 92.4,
                  26: 91.8, 22: 91.2, 18: 90.7, 14: 90.1, 10: 89.5, 6: 88.9, 2: 88.3,
                  -2: 87.6, -6: 87.0, -10: 86.4, -14: 85.8, -18: 85.2, -22: 84.5, -26: 83.9,
                  -30: 83.2, -34: 82.6, -38: 81.9, -42: 81.3, -46: 80.6, -50: 79.9, -54: 79.2,},
            1000: {54: 91.9, 50: 92.6, 46: 93.2, 42: 93.8, 38: 94.5, 34: 94.2, 30: 93.6,
                  26: 93.0, 22: 92.4, 18: 91.8, 14: 91.2, 10: 90.6, 6: 90.0, 2: 89.4,
                  -2: 88.8, -6: 88.2, -10: 87.5, -14: 86.9, -18: 86.3, -22: 85.6, -26: 85.0,
                  -30: 84.3, -34: 83.6, -38: 83.0, -42: 82.3, -46: 81.6, -50: 80.9, -54: 80.3,},
            2000: {50: 92.9, 46: 93.4, 42: 94.0, 38: 94.6, 34: 94.8, 30: 94.2, 26: 93.6,
                  22: 93.0, 18: 92.4, 14: 91.8, 10: 91.2, 6: 90.6, 2: 90.0, -2: 89.4,
                  -6: 88.8, -10: 88.1, -14: 87.5, -18: 86.8, -22: 86.2, -26: 85.5, -30: 84.9,
                  -34: 84.2, -38: 83.6, -42: 82.9, -46: 82.2, -50: 81.5, -54: 80.8,},
            3000: {50: 94.4, 46: 94.8, 42: 95.3, 38: 95.4, 34: 94.8, 30: 94.2, 26: 93.6,
                  22: 93.0, 18: 92.4, 14: 91.7, 10: 91.1, 6: 90.5, 2: 89.9, -2: 89.2,
                  -6: 88.6, -10: 87.9, -14: 87.3, -18: 86.6, -22: 85.9, -26: 85.3, -30: 84.6,
                  -34: 83.9, -38: 83.2, -42: 82.5, -46: 81.8,},
            4000: {42: 94.4, 38: 94.8, 34: 95.3, 30: 95.4, 26: 94.8, 22: 94.2, 18: 93.6,
                  14: 93.0, 10: 92.4, 6: 91.7, 2: 91.1, -2: 90.5, -6: 89.9, -10: 89.2,
                  -14: 88.6, -18: 87.9, -22: 87.3, -26: 86.6, -30: 85.9, -34: 85.3, -38: 84.6,
                  -42: 83.9, -46: 83.2, -50: 82.5, -54: 81.8,},
            5000: {42: 94.7, 38: 95.2, 34: 95.6, 30: 96.0, 26: 95.6, 22: 95.0, 18: 94.4,
                  14: 93.8, 10: 93.1, 6: 92.5, 2: 91.9, -2: 91.2, -6: 90.6, -10: 90.0,
                  -14: 89.3, -18: 88.7, -22: 88.0, -26: 87.3, -30: 86.7, -34: 86.0, -38: 85.3,
                  -42: 84.6, -46: 83.9, -50: 83.2, -54: 82.5,},
            6000: {42: 95.0, 38: 95.4, 34: 95.9, 30: 96.2, 26: 96.4, 22: 95.7, 18: 95.1,
                  14: 94.5, 10: 93.9, 6: 93.3, 2: 92.6, -2: 92.0, -6: 91.3, -10: 90.7,
                  -14: 90.0, -18: 89.4, -22: 88.7, -26: 88.0, -30: 87.3, -34: 86.7, -38: 86.0,
                  -42: 85.3, -46: 84.6, -50: 83.9, -54: 83.2,},
            7000: {42: 95.3, 38: 95.7, 34: 96.2, 30: 96.6, 26: 97.0, 22: 96.6, 18: 96.0,
                  14: 95.3, 10: 94.7, 6: 94.1, 2: 93.4, -2: 92.8, -6: 92.1, -10: 91.5,
                  -14: 90.8, -18: 90.1, -22: 89.5, -26: 88.8, -30: 88.1, -34: 87.4, -38: 86.7,
                  -42: 86.0, -46: 85.3, -50: 84.6, -54: 83.9,},
            8000: {38: 95.7, 34: 96.4, 30: 96.7, 26: 97.0, 22: 97.1, 18: 96.5, 14: 95.8,
                  10: 95.2, 6: 94.6, 2: 93.9, -2: 93.3, -6: 92.6, -10: 92.0, -14: 91.3,
                  -18: 90.6, -22: 89.9, -26: 89.3, -30: 88.6, -34: 87.9, -38: 87.2, -42: 86.5,
                  -46: 85.8, -50: 85.1, -54: 84.3,},
        },
    },
    # ('A21N', 'LEAP-1A'):  N1 — awaiting capture (A321neo is A21N, not A321)
}


def _interp_curve(curve, x, max_gap, max_extrap=None):
    """Linear interpolation along one temperature curve ({temp: value}).
    Returns None when the bracketing samples are further apart than
    max_gap, rather than drawing a straight line across an unmeasured
    stretch.

    Past either end it clamps, which suits a hand-measured curve whose
    ends are just where sampling stopped. Pass max_extrap to bound that: a
    published table is ragged where the ENVELOPE ends, not where someone
    got tired, so clamping one sideways into an uncertified corner invents
    a certified-looking number. Beyond max_extrap past the last sample,
    return nothing instead."""
    if not curve:
        return None
    if x in curve:          # exact sample — never subject to the gap guard
        return curve[x]
    ks = sorted(curve)
    if x <= ks[0]:
        return None if max_extrap is not None and (ks[0] - x) > max_extrap else curve[ks[0]]
    if x >= ks[-1]:
        return None if max_extrap is not None and (x - ks[-1]) > max_extrap else curve[ks[-1]]
    for a, b in zip(ks, ks[1:]):
        if a <= x <= b:
            if max_gap is not None and (b - a) > max_gap:
                return None
            return curve[a] + (curve[b] - curve[a]) * ((x - a) / (b - a))
    return None



# ---------------------------------------------------------------------------
# 757/767 takeoff thrust
#
# Source: X-Plane's Boeing767-Extended perfData/<variant>/toga.txt —
# transcribed, then CHECKED. All 91 cells of the RB211-535E4 grid match the
# ATI 757-200 FCOM Vol 1, PI.10.10 "Takeoff EPR", exactly: same altitudes
# (-1000..10000), same OATs (70..10 & below), same values. So this is not
# sim-invented data, whatever its file lived in — for that variant it is the
# real manual. The other nine share its lineage but have not been checked
# against a manual; if one ever disagrees with a real FCOM, the FCOM wins.
#
# The FCOM's base table is for PACKS ON, ANTI-ICE OFF. It also publishes
# bleed corrections (packs off +0.01; engine anti-ice 0.00 to -0.01; engine
# & wing anti-ice -0.01 to -0.02, by altitude band) which are NOT applied
# here — get_takeoff_thrust has no bleed argument beyond apu_on, and adding
# one is a change to its signature rather than to this table.
#
# One grid answers both TOGA and FLEX, and the FCOM says so: PI.10.10 puts
# "Assumed Temperature Reduced Thrust" on the same page as the takeoff EPR
# grid, with no second grid — you read this one at the assumed temperature.
# What it adds is a floor: a MIN TAKEOFF EPR ALLOWED for each actual-OAT
# max EPR, at 25% thrust reduction. That floor is recorded on the 535E4
# entry as 'flex_min_epr' (the FULL column; TO1/TO2 derates are not modelled
# here) and applied by get_takeoff_thrust. The other variants have no floor
# because no manual for them has been read — better nothing than a limit
# invented by analogy.
#
# No flex_oat_coeff on the GE entries. The Airbus CFM tables needed one
# because their flex rows were not even monotonic in assumed temperature
# without it; there is no equivalent evidence here, and inventing a
# correction to match a shape nobody has measured would be a curve fit
# dressed as physics.
#
# GE is rated in N1, PW and RR in EPR — the same split the A320 family has
# between CFM and IAE.
# ---------------------------------------------------------------------------
BOEING_TAKEOFF_THRUST = {
    ('B752', 'PW2037'): {
        'param': 'EPR',
        # 752PW/toga.txt — 8 pressure altitudes x 13 OATs, 1000ft steps
        'max_alt_gap': 2500,
        'max_temp_gap': 6,
        'alt_snap': 500,
        'toga': {
            0: {70: 1.2, 65: 1.24, 60: 1.26, 55: 1.28, 50: 1.32, 45: 1.35, 40: 1.37, 35: 1.39, 30: 1.41, 25: 1.41, 20: 1.41, 15: 1.41, 10: 1.41},
            1000: {70: 1.21, 65: 1.24, 60: 1.26, 55: 1.28, 50: 1.32, 45: 1.35, 40: 1.38, 35: 1.4, 30: 1.42, 25: 1.43, 20: 1.43, 15: 1.43, 10: 1.43},
            2000: {70: 1.21, 65: 1.24, 60: 1.26, 55: 1.28, 50: 1.32, 45: 1.35, 40: 1.38, 35: 1.41, 30: 1.43, 25: 1.45, 20: 1.45, 15: 1.45, 10: 1.45},
            3000: {70: 1.22, 65: 1.24, 60: 1.26, 55: 1.28, 50: 1.32, 45: 1.35, 40: 1.39, 35: 1.41, 30: 1.44, 25: 1.47, 20: 1.47, 15: 1.47, 10: 1.47},
            4000: {70: 1.22, 65: 1.24, 60: 1.26, 55: 1.28, 50: 1.32, 45: 1.35, 40: 1.39, 35: 1.42, 30: 1.45, 25: 1.48, 20: 1.49, 15: 1.49, 10: 1.49},
            5000: {70: 1.23, 65: 1.24, 60: 1.26, 55: 1.28, 50: 1.32, 45: 1.35, 40: 1.39, 35: 1.43, 30: 1.45, 25: 1.49, 20: 1.51, 15: 1.51, 10: 1.51},
            6000: {70: 1.23, 65: 1.24, 60: 1.26, 55: 1.28, 50: 1.32, 45: 1.35, 40: 1.39, 35: 1.43, 30: 1.45, 25: 1.49, 20: 1.52, 15: 1.53, 10: 1.53},
            8000: {70: 1.23, 65: 1.24, 60: 1.26, 55: 1.28, 50: 1.32, 45: 1.35, 40: 1.39, 35: 1.43, 30: 1.45, 25: 1.49, 20: 1.52, 15: 1.55, 10: 1.56},
        },
    },
    ('B752', 'RB211-535E4'): {
        'param': 'EPR',
        # ATI FCOM PI.10.10, Assumed Temperature Reduced Thrust (25%
        # reduction), FULL column: max takeoff EPR for the ACTUAL OAT ->
        # minimum EPR a flex setting may be reduced to.
        'flex_min_epr': {1.80: 1.60, 1.75: 1.56, 1.70: 1.53,
                         1.65: 1.49, 1.60: 1.45, 1.55: 1.41},
        # ATI FCOM PI.10.10, "EPR Adjustments for Engine Bleeds". The grid
        # itself is packs ON, anti-ice OFF; these are the deltas from that.
        # Banded by pressure altitude, and the band edge really is 8000/8001
        # in the manual — (ceiling, delta), first band whose ceiling the
        # altitude is at or below.
        'bleed_corrections': {
            'packs_off':            [(8000, 0.01), (None, 0.01)],
            'engine_anti_ice':      [(8000, 0.00), (None, -0.01)],
            'engine_wing_anti_ice': [(8000, -0.01), (None, -0.02)],
        },
        # 752RR/toga.txt — 7 pressure altitudes x 13 OATs, 1000ft steps
        'max_alt_gap': 2500,
        'max_temp_gap': 6,
        'alt_snap': 500,
        'toga': {
            -1000: {70: 1.47, 65: 1.51, 60: 1.54, 55: 1.57, 50: 1.6, 45: 1.63, 40: 1.66, 35: 1.68, 30: 1.71, 25: 1.71, 20: 1.71, 15: 1.71, 10: 1.71},
            0: {70: 1.47, 65: 1.51, 60: 1.54, 55: 1.57, 50: 1.6, 45: 1.63, 40: 1.65, 35: 1.68, 30: 1.71, 25: 1.72, 20: 1.72, 15: 1.72, 10: 1.72},
            2000: {70: 1.47, 65: 1.5, 60: 1.54, 55: 1.57, 50: 1.6, 45: 1.63, 40: 1.66, 35: 1.68, 30: 1.71, 25: 1.74, 20: 1.74, 15: 1.74, 10: 1.74},
            4000: {70: 1.47, 65: 1.5, 60: 1.53, 55: 1.56, 50: 1.6, 45: 1.63, 40: 1.66, 35: 1.69, 30: 1.72, 25: 1.74, 20: 1.76, 15: 1.76, 10: 1.76},
            6000: {70: 1.47, 65: 1.5, 60: 1.53, 55: 1.56, 50: 1.6, 45: 1.63, 40: 1.66, 35: 1.69, 30: 1.72, 25: 1.74, 20: 1.77, 15: 1.78, 10: 1.78},
            8000: {70: 1.47, 65: 1.5, 60: 1.53, 55: 1.57, 50: 1.6, 45: 1.63, 40: 1.66, 35: 1.69, 30: 1.72, 25: 1.74, 20: 1.76, 15: 1.78, 10: 1.79},
            10000: {70: 1.47, 65: 1.5, 60: 1.53, 55: 1.56, 50: 1.58, 45: 1.61, 40: 1.64, 35: 1.67, 30: 1.7, 25: 1.72, 20: 1.75, 15: 1.77, 10: 1.79},
        },
    },
    ('B753', 'PW2040'): {
        'param': 'EPR',
        # 753PW/toga.txt — 8 pressure altitudes x 13 OATs, 1000ft steps
        'max_alt_gap': 2500,
        'max_temp_gap': 6,
        'alt_snap': 500,
        'toga': {
            0: {70: 1.2, 65: 1.24, 60: 1.26, 55: 1.28, 50: 1.32, 45: 1.35, 40: 1.37, 35: 1.39, 30: 1.41, 25: 1.41, 20: 1.41, 15: 1.41, 10: 1.41},
            1000: {70: 1.21, 65: 1.24, 60: 1.26, 55: 1.28, 50: 1.32, 45: 1.35, 40: 1.38, 35: 1.4, 30: 1.42, 25: 1.43, 20: 1.43, 15: 1.43, 10: 1.43},
            2000: {70: 1.21, 65: 1.24, 60: 1.26, 55: 1.28, 50: 1.32, 45: 1.35, 40: 1.38, 35: 1.41, 30: 1.43, 25: 1.45, 20: 1.45, 15: 1.45, 10: 1.45},
            3000: {70: 1.22, 65: 1.24, 60: 1.26, 55: 1.28, 50: 1.32, 45: 1.35, 40: 1.39, 35: 1.41, 30: 1.44, 25: 1.47, 20: 1.47, 15: 1.47, 10: 1.47},
            4000: {70: 1.22, 65: 1.24, 60: 1.26, 55: 1.28, 50: 1.32, 45: 1.35, 40: 1.39, 35: 1.42, 30: 1.45, 25: 1.48, 20: 1.49, 15: 1.49, 10: 1.49},
            5000: {70: 1.23, 65: 1.24, 60: 1.26, 55: 1.28, 50: 1.32, 45: 1.35, 40: 1.39, 35: 1.43, 30: 1.45, 25: 1.49, 20: 1.51, 15: 1.51, 10: 1.51},
            6000: {70: 1.23, 65: 1.24, 60: 1.26, 55: 1.28, 50: 1.32, 45: 1.35, 40: 1.39, 35: 1.43, 30: 1.45, 25: 1.49, 20: 1.52, 15: 1.53, 10: 1.53},
            8000: {70: 1.23, 65: 1.24, 60: 1.26, 55: 1.28, 50: 1.32, 45: 1.35, 40: 1.39, 35: 1.43, 30: 1.45, 25: 1.49, 20: 1.52, 15: 1.55, 10: 1.56},
        },
    },
    ('B753', 'RB211-535E4-B'): {
        'param': 'EPR',
        # 753RR/toga.txt — 7 pressure altitudes x 13 OATs, 1000ft steps
        'max_alt_gap': 2500,
        'max_temp_gap': 6,
        'alt_snap': 500,
        'toga': {
            -1000: {70: 1.47, 65: 1.51, 60: 1.54, 55: 1.57, 50: 1.6, 45: 1.63, 40: 1.66, 35: 1.68, 30: 1.71, 25: 1.71, 20: 1.71, 15: 1.71, 10: 1.71},
            0: {70: 1.47, 65: 1.51, 60: 1.54, 55: 1.57, 50: 1.6, 45: 1.63, 40: 1.65, 35: 1.68, 30: 1.71, 25: 1.72, 20: 1.72, 15: 1.72, 10: 1.72},
            2000: {70: 1.47, 65: 1.5, 60: 1.54, 55: 1.57, 50: 1.6, 45: 1.63, 40: 1.66, 35: 1.68, 30: 1.71, 25: 1.74, 20: 1.74, 15: 1.74, 10: 1.74},
            4000: {70: 1.47, 65: 1.5, 60: 1.53, 55: 1.56, 50: 1.6, 45: 1.63, 40: 1.66, 35: 1.69, 30: 1.72, 25: 1.74, 20: 1.76, 15: 1.76, 10: 1.76},
            6000: {70: 1.47, 65: 1.5, 60: 1.53, 55: 1.56, 50: 1.6, 45: 1.63, 40: 1.66, 35: 1.69, 30: 1.72, 25: 1.74, 20: 1.77, 15: 1.78, 10: 1.78},
            8000: {70: 1.47, 65: 1.5, 60: 1.53, 55: 1.57, 50: 1.6, 45: 1.63, 40: 1.66, 35: 1.69, 30: 1.72, 25: 1.74, 20: 1.76, 15: 1.78, 10: 1.79},
            10000: {70: 1.47, 65: 1.5, 60: 1.53, 55: 1.56, 50: 1.58, 45: 1.61, 40: 1.64, 35: 1.67, 30: 1.7, 25: 1.72, 20: 1.75, 15: 1.77, 10: 1.79},
        },
    },
    ('B762', 'CF6-80A'): {
        'param': 'N1',
        # 762GE/toga.txt — 6 pressure altitudes x 15 OATs, 1000ft steps
        'max_alt_gap': 2500,
        'max_temp_gap': 6,
        'alt_snap': 500,
        'toga': {
            -1000: {55: 102.3, 50: 103.5, 45: 104.2, 40: 104.7, 35: 105, 30: 104.2, 25: 103.3, 20: 102.5, 15: 101.6, 10: 100.7, 0: 98.9, -10: 97.1, -20: 95.3, -30: 93.4, -40: 91.4},
            0: {55: 103.1, 50: 104.4, 45: 105, 40: 105.5, 35: 106.1, 30: 105.7, 25: 104.8, 20: 103.9, 15: 103, 10: 102.1, 0: 100.3, -10: 98.4, -20: 96.6, -30: 94.6, -40: 92.7},
            2000: {55: 103.3, 50: 104.6, 45: 105.1, 40: 105.6, 35: 106.2, 30: 106.8, 25: 105.9, 20: 105, 15: 104.1, 10: 103.2, 0: 101.4, -10: 99.5, -20: 97.6, -30: 95.7, -40: 93.7},
            4000: {55: 103.3, 50: 104.6, 45: 105.1, 40: 105.5, 35: 106.2, 30: 106.9, 25: 107.3, 20: 106.3, 15: 105.4, 10: 104.6, 0: 102.6, -10: 100.8, -20: 98.9, -30: 96.9, -40: 94.9},
            6000: {55: 103.3, 50: 104.6, 45: 105, 40: 105.4, 35: 106.1, 30: 106.8, 25: 107.6, 20: 107.9, 15: 106.9, 10: 106, 0: 104.2, -10: 102.2, -20: 100.2, -30: 98.2, -40: 96.2},
            8000: {55: 103.3, 50: 104.6, 45: 105, 40: 105.2, 35: 105.8, 30: 106.5, 25: 107.6, 20: 108.5, 15: 108.4, 10: 107.5, 0: 105.6, -10: 103.6, -20: 101.6, -30: 99.6, -40: 97.6},
        },
    },
    ('B762', 'PW4056'): {
        'param': 'EPR',
        # 762PW/toga.txt — 6 pressure altitudes x 13 OATs, 1000ft steps
        'max_alt_gap': 2500,
        'max_temp_gap': 6,
        'alt_snap': 500,
        'toga': {
            -1000: {70: 1.26, 65: 1.29, 60: 1.33, 55: 1.36, 50: 1.4, 45: 1.43, 40: 1.47, 35: 1.49, 30: 1.49, 25: 1.49, 20: 1.49, 15: 1.49, 10: 1.49},
            0: {70: 1.26, 65: 1.29, 60: 1.33, 55: 1.36, 50: 1.4, 45: 1.43, 40: 1.47, 35: 1.5, 30: 1.52, 25: 1.52, 20: 1.52, 15: 1.52, 10: 1.52},
            2000: {70: 1.26, 65: 1.29, 60: 1.32, 55: 1.36, 50: 1.4, 45: 1.43, 40: 1.47, 35: 1.5, 30: 1.54, 25: 1.54, 20: 1.54, 15: 1.54, 10: 1.54},
            4000: {70: 1.26, 65: 1.29, 60: 1.32, 55: 1.36, 50: 1.39, 45: 1.43, 40: 1.47, 35: 1.5, 30: 1.54, 25: 1.57, 20: 1.57, 15: 1.57, 10: 1.57},
            6000: {70: 1.26, 65: 1.29, 60: 1.32, 55: 1.36, 50: 1.39, 45: 1.43, 40: 1.47, 35: 1.5, 30: 1.53, 25: 1.57, 20: 1.6, 15: 1.6, 10: 1.6},
            8000: {70: 1.26, 65: 1.29, 60: 1.32, 55: 1.36, 50: 1.39, 45: 1.43, 40: 1.47, 35: 1.5, 30: 1.53, 25: 1.57, 20: 1.6, 15: 1.63, 10: 1.63},
        },
    },
    ('B763', 'CF6-80C2B6F'): {
        'param': 'N1',
        # 767-300 / CF6-80C2B6F FCOM, PI.10.14 "Takeoff %N1" — transcribed
        # from the manual, not from the sim file. The X-Plane copy of this
        # table is abridged: no 9000ft column, no -20/-30/-40 rows, and
        # 109.7 where the manual says 109.6 at 8000ft/30C.
        'max_alt_gap': 2500,
        'max_temp_gap': 6,
        'alt_snap': 1000,
        # "Based on engine bleed for packs on, EEC NORM or ALTN and anti-ice
        # ON OR OFF" — anti-ice does not move N1 on this engine, so there is
        # no anti-ice correction here, only packs.
        'bleed_corrections': {
            'packs_off': [(2000, 0.3), (4000, 0.4), (None, 0.5)],
        },
        # PI.10.18, TO1 = 10% thrust reduction. A derate is a different
        # rating, not a reduction applied to this grid, so it gets its own.
        'derates': {
            'TO1': {
                -1000: {55: 103.6, 50: 104.4, 45: 105, 40: 105.5, 35: 105.8, 30: 105.7, 25: 104.8, 20: 103.9, 15: 103.1, 10: 102.2, 0: 100.4, -10: 98.6, -20: 96.7, -30: 94.8, -40: 92.9, -50: 90.9},
                0: {55: 103.6, 50: 104.4, 45: 105, 40: 105.4, 35: 105.8, 30: 106.2, 25: 105.3, 20: 104.4, 15: 103.6, 10: 102.7, 0: 100.9, -10: 99, -20: 97.2, -30: 95.3, -40: 93.3, -50: 91.3},
                2000: {55: 103.4, 50: 104.4, 45: 105, 40: 105.5, 35: 105.8, 30: 106.2, 25: 106.4, 20: 105.5, 15: 104.6, 10: 103.7, 0: 101.9, -10: 100.1, -20: 98.2, -30: 96.3, -40: 94.3, -50: 92.3},
                4000: {55: 103, 50: 104, 45: 104.8, 40: 105.3, 35: 105.7, 30: 106.1, 25: 106.6, 20: 106.4, 15: 105.5, 10: 104.6, 0: 102.8, -10: 100.9, -20: 99.1, -30: 97.1, -40: 95.2, -50: 93.2},
                6000: {55: 102.7, 50: 103.6, 45: 104.6, 40: 105.2, 35: 105.6, 30: 106.1, 25: 106.5, 20: 106.9, 15: 106.4, 10: 105.5, 0: 103.7, -10: 101.8, -20: 99.9, -30: 98, -40: 96, -50: 94},
                8000: {55: 102.5, 50: 103.4, 45: 104.4, 40: 105.1, 35: 105.6, 30: 106.1, 25: 106.6, 20: 106.9, 15: 107.1, 10: 106.4, 0: 104.5, -10: 102.6, -20: 100.7, -30: 98.8, -40: 96.8, -50: 94.7},
            },
        },
        'toga': {
            -1000: {55: 106.1, 50: 106.8, 45: 107.5, 40: 108.2, 35: 109, 30: 109.1, 25: 108.2, 20: 107.3, 15: 106.4, 10: 105.5, 0: 103.6, -10: 101.7, -20: 99.8, -30: 97.9, -40: 95.9, -50: 93.8},
            0: {55: 106.1, 50: 106.9, 45: 107.5, 40: 108.2, 35: 108.9, 30: 109.8, 25: 108.9, 20: 108, 15: 107.1, 10: 106.2, 0: 104.3, -10: 102.4, -20: 100.4, -30: 98.5, -40: 96.4, -50: 94.4},
            2000: {55: 105.9, 50: 106.8, 45: 107.5, 40: 108.2, 35: 109, 30: 109.8, 25: 110.3, 20: 109.4, 15: 108.5, 10: 107.5, 0: 105.7, -10: 103.8, -20: 101.8, -30: 99.8, -40: 97.8, -50: 95.8},
            4000: {55: 105.5, 50: 106.5, 45: 107.3, 40: 108, 35: 108.8, 30: 109.7, 25: 110.6, 20: 110.6, 15: 109.7, 10: 108.8, 0: 106.9, -10: 104.9, -20: 103, -30: 101, -40: 99, -50: 96.9},
            6000: {55: 105.1, 50: 106.1, 45: 107, 40: 107.9, 35: 108.7, 30: 109.6, 25: 110.5, 20: 111.3, 15: 111, 10: 110, 0: 108.1, -10: 106.1, -20: 104.2, -30: 102.2, -40: 100.1, -50: 98},
            8000: {55: 104.9, 50: 105.9, 45: 106.9, 40: 107.8, 35: 108.7, 30: 109.6, 25: 110.6, 20: 111.4, 15: 112, 10: 111.3, 0: 109.4, -10: 107.4, -20: 105.4, -30: 103.4, -40: 101.3, -50: 99.2},
            9000: {55: 104.7, 50: 105.7, 45: 106.6, 40: 107.7, 35: 108.6, 30: 109.6, 25: 110.6, 20: 111.3, 15: 112.1, 10: 112.1, 0: 110.1, -10: 108.1, -20: 106.1, -30: 104, -40: 102, -50: 99.8},
        },
    },
    ('B763', 'PW4060'): {
        'param': 'EPR',
        # 763PW/toga.txt — 6 pressure altitudes x 13 OATs, 1000ft steps
        'max_alt_gap': 2500,
        'max_temp_gap': 6,
        'alt_snap': 500,
        'toga': {
            -1000: {70: 1.24, 65: 1.29, 60: 1.34, 55: 1.39, 50: 1.43, 45: 1.46, 40: 1.51, 35: 1.53, 30: 1.53, 25: 1.53, 20: 1.53, 15: 1.53, 10: 1.53},
            0: {70: 1.23, 65: 1.28, 60: 1.33, 55: 1.38, 50: 1.42, 45: 1.46, 40: 1.51, 35: 1.55, 30: 1.56, 25: 1.56, 20: 1.56, 15: 1.56, 10: 1.56},
            2000: {70: 1.25, 65: 1.27, 60: 1.33, 55: 1.38, 50: 1.42, 45: 1.46, 40: 1.51, 35: 1.55, 30: 1.58, 25: 1.58, 20: 1.58, 15: 1.58, 10: 1.58},
            4000: {70: 1.25, 65: 1.27, 60: 1.32, 55: 1.37, 50: 1.42, 45: 1.46, 40: 1.51, 35: 1.54, 30: 1.58, 25: 1.6, 20: 1.6, 15: 1.6, 10: 1.6},
            6000: {70: 1.25, 65: 1.27, 60: 1.32, 55: 1.37, 50: 1.42, 45: 1.46, 40: 1.5, 35: 1.54, 30: 1.58, 25: 1.6, 20: 1.61, 15: 1.61, 10: 1.61},
            8000: {70: 1.25, 65: 1.27, 60: 1.32, 55: 1.37, 50: 1.42, 45: 1.46, 40: 1.51, 35: 1.54, 30: 1.58, 25: 1.6, 20: 1.62, 15: 1.64, 10: 1.64},
        },
    },
    ('B763', 'RB211-524H'): {
        'param': 'EPR',
        # 763RR/toga.txt — 6 pressure altitudes x 13 OATs, 1000ft steps
        'max_alt_gap': 2500,
        'max_temp_gap': 6,
        'alt_snap': 500,
        'toga': {
            -1000: {70: 1.47, 65: 1.51, 60: 1.55, 55: 1.58, 50: 1.62, 45: 1.66, 40: 1.69, 35: 1.73, 30: 1.74, 25: 1.74, 20: 1.74, 15: 1.74, 10: 1.74},
            0: {70: 1.47, 65: 1.51, 60: 1.55, 55: 1.58, 50: 1.62, 45: 1.66, 40: 1.69, 35: 1.73, 30: 1.75, 25: 1.75, 20: 1.75, 15: 1.75, 10: 1.75},
            2000: {70: 1.47, 65: 1.51, 60: 1.55, 55: 1.58, 50: 1.62, 45: 1.66, 40: 1.69, 35: 1.73, 30: 1.75, 25: 1.77, 20: 1.77, 15: 1.77, 10: 1.77},
            4000: {70: 1.47, 65: 1.51, 60: 1.55, 55: 1.58, 50: 1.62, 45: 1.66, 40: 1.69, 35: 1.73, 30: 1.75, 25: 1.77, 20: 1.78, 15: 1.78, 10: 1.78},
            6000: {70: 1.47, 65: 1.51, 60: 1.55, 55: 1.58, 50: 1.62, 45: 1.66, 40: 1.69, 35: 1.73, 30: 1.75, 25: 1.77, 20: 1.78, 15: 1.78, 10: 1.79},
            8000: {70: 1.47, 65: 1.51, 60: 1.55, 55: 1.58, 50: 1.62, 45: 1.66, 40: 1.69, 35: 1.73, 30: 1.75, 25: 1.77, 20: 1.78, 15: 1.78, 10: 1.79},
        },
    },
    ('B764', 'CF6-80C2B8F'): {
        'param': 'N1',
        # 764GE/toga.txt — 6 pressure altitudes x 13 OATs, 1000ft steps
        'max_alt_gap': 2500,
        'max_temp_gap': 6,
        'alt_snap': 500,
        'toga': {
            -1000: {55: 106.1, 50: 106.8, 45: 107.5, 40: 108.2, 35: 109, 30: 109.1, 25: 108.2, 20: 107.3, 15: 106.4, 10: 105.5, 0: 103.6, -10: 101.7, -50: 93.8},
            0: {55: 106.1, 50: 106.9, 45: 107.5, 40: 108.2, 35: 108.9, 30: 109.8, 25: 108.9, 20: 108, 15: 107.1, 10: 106.2, 0: 104.3, -10: 102.4, -50: 94.4},
            2000: {55: 105.9, 50: 106.8, 45: 107.5, 40: 108.2, 35: 109, 30: 109.8, 25: 110.3, 20: 109.4, 15: 108.5, 10: 107.5, 0: 105.7, -10: 103.8, -50: 95.8},
            4000: {55: 105.5, 50: 106.5, 45: 107.3, 40: 108, 35: 108.8, 30: 109.7, 25: 110.6, 20: 110.6, 15: 109.7, 10: 108.8, 0: 106.9, -10: 104.9, -50: 96.9},
            6000: {55: 105.1, 50: 106.1, 45: 107, 40: 107.9, 35: 108.7, 30: 109.6, 25: 110.5, 20: 111.3, 15: 111, 10: 110, 0: 108.1, -10: 106.1, -50: 98},
            8000: {55: 104.9, 50: 105.9, 45: 106.9, 40: 107.8, 35: 108.7, 30: 109.7, 25: 110.6, 20: 111.4, 15: 112, 10: 111.3, 0: 109.4, -10: 107.4, -50: 99.2},
        },
    },
}


# Both families answer the same question, so the lookup sees one table.
_TAKEOFF_THRUST = {**AIRBUS_TAKEOFF_THRUST, **BOEING_TAKEOFF_THRUST}


def _interp_grid(grid, temp, altitude, max_alt_gap=None, max_temp_gap=None,
                 alt_snap=None, max_temp_extrap=None, max_alt_extrap=None):
    """Temperature curve within each bracketing altitude, then blend across
    altitude. Returns None if either axis can't be resolved."""
    if not grid:
        return None
    try:
        temp, altitude = float(temp), float(altitude)
    except (TypeError, ValueError):
        return None
    alts = sorted(grid)
    nearest = min(alts, key=lambda a: abs(a - altitude))
    if altitude in grid:
        # An exact altitude is that column, not a blend of it with its
        # neighbour. Without this the bracket search pairs it with the
        # column below and needs BOTH curves to resolve, so one gap in a
        # neighbouring column takes out a cell that is present and exact.
        lo = hi = int(altitude) if float(altitude).is_integer() else altitude
    elif alt_snap is not None and abs(nearest - altitude) <= alt_snap:
        lo = hi = nearest
    elif altitude <= alts[0]:
        if max_alt_extrap is not None and (alts[0] - altitude) > max_alt_extrap:
            return None
        lo = hi = alts[0]
    elif altitude >= alts[-1]:
        if max_alt_extrap is not None and (altitude - alts[-1]) > max_alt_extrap:
            return None
        lo = hi = alts[-1]
    else:
        lo = hi = alts[0]
        for a, b in zip(alts, alts[1:]):
            if a <= altitude <= b:
                lo, hi = a, b
                break
        if max_alt_gap is not None and (hi - lo) > max_alt_gap:
            return None
    v_lo = _interp_curve(grid[lo], temp, max_temp_gap, max_temp_extrap)
    if lo == hi:
        return v_lo
    v_hi = _interp_curve(grid[hi], temp, max_temp_gap, max_temp_extrap)
    if v_lo is None or v_hi is None:
        return None
    return v_lo + (v_hi - v_lo) * ((altitude - lo) / (hi - lo))


def derate_from_thrust_setting(thrust_setting):
    """'D-TO1' -> 'TO1'. SimBrief's TLR names the rating in <thrust_setting>:
    D-TO1/D-TO2 for the derates, TO or TOGA for full thrust.

    Anything else that looks like a derate is returned as-is rather than
    treated as full thrust, so an unrecognised rating reaches
    get_takeoff_thrust, finds no grid, and shows nothing. A blank is
    honest; printing TOGA numbers to a crew who selected a derate is not.
    """
    s = (thrust_setting or '').strip().upper().replace(' ', '')
    if not s or s in ('TO', 'TOGA', 'MAX', 'XXX'):
        return None
    if s.startswith('D-'):
        s = s[2:]
    return s or None


def get_takeoff_thrust(icao_code, engine, oat, altitude, assumed_temp=None, packs_off=False, anti_ice=None,
                       derate=None,
                       apu_on=False):
    """Max (TOGA) or reduced (FLEX) takeoff thrust for an Airbus.

    Pass assumed_temp for FLEX — that selects the flex grid and reads it at
    the assumed temperature. Without it the TOGA grid is read at actual OAT.

    Returns {'param': 'EPR'|'N1', 'value': float, 'engine': ..., 'flex': bool}
    or None when the engine is unrecognised, there's no grid for that
    combination, or the request falls in an unmeasured gap. None always
    means "show nothing", never a substituted value.
    """
    family, param = engine_family(engine)
    if not family:
        return None
    entry = _TAKEOFF_THRUST.get(((icao_code or '').upper(), family))
    if not entry:
        return None
    flex = assumed_temp is not None
    # A table indexed by temperature answers both questions: TOGA is it read
    # at the actual OAT, flex is it read at the assumed temperature. Only a
    # type whose flex grid was measured separately — the Airbus entries —
    # needs one of its own.
    # A derate is a different thrust rating with its own published grid, not
    # a percentage taken off this one — TO1 at 55C reads 103.6 where full
    # thrust reads 106.1, which is not 10% of anything. Unknown derate on a
    # type that has none returns nothing rather than quietly giving full
    # thrust: showing TOGA to someone who asked for TO1 is the dangerous
    # direction.
    _grids = entry
    if derate:
        _grids = (entry.get('derates') or {}).get(str(derate).strip().upper())
        if not _grids:
            return None
        if not isinstance(_grids, dict) or 'toga' not in _grids:
            _grids = {'toga': _grids}

    value = _interp_grid(
        (_grids.get('flex') or _grids.get('toga')) if flex else _grids.get('toga'),
        assumed_temp if flex else oat, altitude,
        entry.get('max_alt_gap'), entry.get('max_temp_gap'),
        entry.get('alt_snap'),
        entry.get('max_temp_extrap'), entry.get('max_alt_extrap'),
    )
    if value is None:
        return None
    # N1-rated engines need the ACTUAL OAT even under flex. N1 is a shaft
    # speed, so the speed that delivers the assumed temperature's thrust
    # still depends on the air actually going through the engine; EPR, a
    # pressure ratio, is near enough independent of it (confirmed on the
    # real AA IAE sheets, where flex 37 reads 1.56 at OAT 18 and 26 alike).
    # Without this term the four real CFM flex rows aren't even monotonic
    # in assumed temp — AT 45 reads 93.2 while AT 46 reads 92.1. Applying
    # it straightens them into a curve of near-constant slope, which is why
    # this is a correction and not a curve fit.
    if flex and entry.get('flex_oat_coeff'):
        if oat is None:
            return None
        value += entry['flex_oat_coeff'] * (oat - entry['flex_oat_ref'])
    # Flex may not be reduced below the manual's floor for the day's actual
    # OAT. Without this a cold day would happily flex to an EPR the FCOM
    # forbids — the reduction is capped at 25% of thrust, not unlimited.
    if flex and entry.get('flex_min_epr') and oat is not None:
        full = _interp_grid(_grids.get('toga'), oat, altitude,
                            entry.get('max_alt_gap'), entry.get('max_temp_gap'),
                            entry.get('alt_snap'))
        if full is not None:
            floors = entry['flex_min_epr']
            key = min(floors, key=lambda k: abs(k - full))
            if abs(key - full) <= 0.03:
                value = max(value, floors[key])

    # Engine-bleed corrections, where the manual publishes them. Only the
    # 535E4 does here, so every other type is unaffected rather than being
    # given a delta borrowed from a different engine.
    _bleeds = entry.get('bleed_corrections') or {}
    if _bleeds and altitude is not None:
        def _band(rows):
            for ceiling, delta in rows:
                if ceiling is None or altitude <= ceiling:
                    return delta
            return 0.0
        if packs_off and 'packs_off' in _bleeds:
            value += _band(_bleeds['packs_off'])
        _ai = (anti_ice or '').strip().lower()
        if _ai in ('engine', 'eng') and 'engine_anti_ice' in _bleeds:
            value += _band(_bleeds['engine_anti_ice'])
        elif _ai in ('engine_wing', 'wing', 'both') and 'engine_wing_anti_ice' in _bleeds:
            value += _band(_bleeds['engine_wing_anti_ice'])

    if apu_on:
        value += (entry.get('bleed_adjust') or {}).get('apu_on', 0.0)
    return {
        'param': entry.get('param', param),
        'value': round(value, 2 if param == 'EPR' else 1),
        'engine': family,
        'flex': flex,
    }
