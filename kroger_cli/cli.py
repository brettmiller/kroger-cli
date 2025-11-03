import configparser
import os
import click
import time
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box
from kroger_cli.api import KrogerAPI
from kroger_cli import helper


class KrogerCLI:
    def __init__(self, config_file='config.ini'):
        self.config_file = config_file
        self.config = configparser.ConfigParser(interpolation=None)
        self.username = None
        self.password = None
        self.console = Console()
        self.api = KrogerAPI(self)
        if not os.path.exists(self.config_file):
            self._init_config_file()
        self.config.read(self.config_file)
        self.init()

    def init(self):
        self.prompt_store_selection()

        if self.username is None and self.config['main']['username'] != '':
            self.username = self.config['main']['username']
            self.password = self.config['main']['password']
        else:
            self.prompt_credentials()

    def prompt_store_selection(self):
        if self.config['main']['username'] != '':
            return

        for store_key in helper.stores:
            store = helper.stores[store_key]
            self.console.print('[bold]' + str(store_key) + '[/bold] - ' + store['label'] + ' (' + store['domain'] + ')')

        selected_store = click.prompt('Please select preferred store', type=int, default=1)
        if selected_store in helper.stores:
            self.config['main']['domain'] = helper.stores[selected_store]['domain']
            self._write_config_file()
        else:
            self.console.print('[bold red]Incorrect entry, please try again.[/bold red]')
            self.prompt_store_selection()

        self.console.rule()

    def prompt_credentials(self):
        self.console.print('In order to continue, please enter your username (email) and password for kroger.com '
                           '(also works with Ralphs, Dillons, Smith\'s and other Kroger\'s Chains)')
        username = click.prompt('Username (email)')
        password = click.prompt('Password', hide_input=True)
        self._set_credentials(username, password)

    def prompt_options(self):
        if self.config['profile']['first_name'] != '':
            self.console.print(Panel('[bold]Welcome Back, ' + self.config['profile']['first_name'] + '! :smiley:\n'
                                     '[dark_blue]Kroger[/dark_blue] CLI[/bold]', box=box.ASCII))
        else:
            self.console.print(Panel('[bold]Welcome to [dark_blue]Kroger[/dark_blue] CLI[/bold] (unofficial command '
                                     'line interface)', box=box.ASCII))

        while True:
            self.console.print('[bold]1[/bold] - Display account info')
            self.console.print('[bold]2[/bold] - Clip all digital coupons')
            self.console.print('[bold]3[/bold] - Purchases Summary')
            self.console.print('[bold]4[/bold] - Points Balance')
            self.console.print('[bold]8[/bold] - Re-Enter username/password')
            self.console.print('[bold]9[/bold] - Exit')
            option = click.prompt('Please select from one of the options', type=int)
            self.console.rule()

            if option == 1:
                self.option_account_info()
            elif option == 2:
                self.option_clip_coupons()
            elif option == 3:
                self.option_purchases_summary()
            elif option == 4:
                self.option_points_balance()
            elif option == 8:
                self.prompt_credentials()
            elif option == 9:
                return

            self.console.rule()
            time.sleep(2)

    def _write_config_file(self):
        with open(self.config_file, 'w') as f:
            self.config.write(f)

    def _init_config_file(self):
        self.config.add_section('main')
        self.config['main']['username'] = ''
        self.config['main']['password'] = ''
        self.config['main']['domain'] = 'kroger.com'
        self.config.add_section('profile')
        self.config['profile']['first_name'] = ''
        self._write_config_file()

    def _set_credentials(self, username, password):
        self.username = username
        self.password = password
        self.config['main']['username'] = self.username
        self.config['main']['password'] = self.password
        self._write_config_file()

    def option_account_info(self):
        info = self.api.get_account_info()
        if info is None:
            self.console.print('[bold red]Couldn\'t retrieve the account info.[/bold red]')
        else:
            self.config = helper.map_account_info(self.config, info)
            self._write_config_file()
            
            # Format and display the account info nicely
            self.console.print('[bold blue]Account Information:[/bold blue]')
            self.console.print('─' * 50)
            
            profile_items = dict(self.config.items(section='profile'))
            
            if profile_items.get('first_name') or profile_items.get('last_name'):
                name = f"{profile_items.get('first_name', '')} {profile_items.get('last_name', '')}".strip()
                if name:
                    self.console.print(f'[green]Name:[/green] {name}')
            
            if profile_items.get('email_address'):
                self.console.print(f'[green]Email:[/green] {profile_items["email_address"]}')
            
            if profile_items.get('loyalty_card_number'):
                self.console.print(f'[green]Loyalty Card:[/green] {profile_items["loyalty_card_number"]}')
                
            if profile_items.get('alt_id'):
                self.console.print(f'[green]Alt ID:[/green] {profile_items["alt_id"]}')
            
            if profile_items.get('mobile_phone'):
                self.console.print(f'[green]Mobile Phone:[/green] {profile_items["mobile_phone"]}')
            
            # Address information
            address_parts = []
            if profile_items.get('address_line1'):
                address_parts.append(profile_items['address_line1'])
            if profile_items.get('address_line2'):
                address_parts.append(profile_items['address_line2'])
            if profile_items.get('city'):
                city_state_zip = profile_items['city']
                if profile_items.get('state'):
                    city_state_zip += f", {profile_items['state']}"
                if profile_items.get('zip'):
                    city_state_zip += f" {profile_items['zip']}"
                address_parts.append(city_state_zip)
            
            if address_parts:
                self.console.print(f'[green]Address:[/green] {", ".join(address_parts)}')
            
            self.console.print('─' * 50)

    def option_points_balance(self):
        balance = self.api.get_points_balance()
        if balance is None:
            self.console.print('[bold red]Couldn\'t retrieve the points balance.[/bold red]')
        elif len(balance) == 1:
            link = 'https://www.' + self.config['main']['domain'] + '/account/dashboard'
            self.console.print('[bold red]Couldn\'t retrieve the points balance. Please visit for more info: '
                               '[link=' + link + ']' + link + '[/link][/bold red]')
        else:
            for i in range(1, len(balance)):
                item = balance[i]
                self.console.print(item['programDisplayInfo']['loyaltyProgramName'] + ': '
                                   '[bold]' + item['programBalance']['balanceDescription'] + '[/bold]')

    def option_clip_coupons(self):
        self.api.clip_coupons()

    def option_purchases_summary(self):
        purchases = self.api.get_purchases_summary()
        if purchases is None:
            self.console.print('[bold red]Couldn\'t retrieve the purchases.[/bold red]')
        else:
            data = helper.process_purchases_summary(purchases)
            if data is not None:
                total = data['total']
                table = Table(title='Purchases Summary (' + data['first_purchase']['transactionTime'][:10] + ' to ' + data['last_purchase']['transactionTime'][:10] + ')')
                table.add_column('Year')
                table.add_column('Store Visits')
                table.add_column('Dollars Spent')
                table.add_column('Dollars Saved')

                for key, year in data['years'].items():
                    table.add_row(str(key), str(year['store_visits']), str(f'${year["total"]:.2f}'), str(f'${year["total_savings"]:.2f}'))
                table.add_row('Total', str(total['store_visits']), str(f'${total["total"]:.2f}'), str(f'${total["total_savings"]:.2f}'))

                self.console.print(table)
