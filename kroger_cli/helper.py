stores = {
    1: {
        'label': 'Kroger',
        'domain': 'kroger.com'
    },
    2: {
        'label': 'Ralphs',
        'domain': 'ralphs.com'
    },
    3: {
        'label': 'Baker’s',
        'domain': 'bakersplus.com'
    },
    4: {
        'label': 'City Market',
        'domain': 'citymarket.com'
    },
    5: {
        'label': 'Dillons',
        'domain': 'dillons.com'
    },
    6: {
        'label': 'Food 4 Less',
        'domain': 'food4less.com'
    },
    7: {
        'label': 'Fred Meyer',
        'domain': 'fredmeyer.com'
    },
    8: {
        'label': 'Fry’s',
        'domain': 'frysfood.com'
    },
    9: {
        'label': 'Smith’s Food and Drug',
        'domain': 'smithsfoodanddrug.com'
    },
    10: {
        'label': 'King Soopers',
        'domain': 'kingsoopers.com'
    },
    11: {
        'label': 'Mariano’s Fresh Market',
        'domain': 'marianos.com'
    },
    12: {
        'label': 'QFC (Quality Food Centers)',
        'domain': 'qfc.com'
    },
    13: {
        'label': 'Metro Market',
        'domain': 'metromarket.net'
    },
    14: {
        'label': 'Pick n Save',
        'domain': 'picknsave.com'
    }
}


def get_config():
    config = configparser.ConfigParser()
    config.read('config.ini')

    return config


def process_purchases_summary(purchases):
    default_dict = {
        'total': 0.00,
        'total_savings': 0.00,
        'store_visits': 0,
    }
    years = {}
    total = dict(default_dict)
    first_purchase = None
    last_purchase = None

    for purchase in purchases:
        if first_purchase is None:
            first_purchase = purchase

        last_purchase = purchase

        year = int(purchase['transactionTime'][:4])
        if year not in years:
            years[year] = dict(default_dict)

        if 'total' in purchase:
            years[year]['total'] += purchase['total']
            years[year]['store_visits'] += 1
            total['total'] += purchase['total']
            total['store_visits'] += 1

        if 'totalSavings' in purchase:
            years[year]['total_savings'] += purchase['totalSavings']
            total['total_savings'] += purchase['totalSavings']

    if last_purchase is None:
        return None

    return {
        'years': years,
        'total': total,
        'first_purchase': first_purchase,
        'last_purchase': last_purchase,
    }


def map_account_info(config, account_info):
    # Handle case where account_info is None (authentication failed)
    if account_info is None:
        return config
        
    # Handle case where account_info doesn't have expected fields
    if not isinstance(account_info, dict):
        return config
        
    if account_info.get('firstName'):
        config['profile']['first_name'] = account_info['firstName']
    if account_info.get('lastName'):
        config['profile']['last_name'] = account_info['lastName']
    if account_info.get('emailAddress'):
        config['profile']['email_address'] = account_info['emailAddress']
    if account_info.get('loyaltyCardNumber'):
        config['profile']['loyalty_card_number'] = account_info['loyaltyCardNumber']
    if account_info.get('altId'):
        config['profile']['alt_id'] = account_info['altId']
    if account_info.get('mobilePhoneNumber'):
        config['profile']['mobile_phone'] = account_info['mobilePhoneNumber']

    if account_info.get('address') and isinstance(account_info['address'], dict):
        if account_info['address'].get('addressLine1'):
            config['profile']['address_line1'] = account_info['address']['addressLine1']
        if account_info['address'].get('addressLine2'):
            config['profile']['address_line2'] = account_info['address']['addressLine2']
        if account_info['address'].get('city'):
            config['profile']['city'] = account_info['address']['city']
        if account_info['address'].get('stateCode'):
            config['profile']['state'] = account_info['address']['stateCode']
        if account_info['address'].get('zip'):
            config['profile']['zip'] = account_info['address']['zip']

    return config
