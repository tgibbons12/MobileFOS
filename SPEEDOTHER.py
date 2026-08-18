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
