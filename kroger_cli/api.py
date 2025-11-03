import asyncio
import json
import re
import datetime
import kroger_cli.cli
from kroger_cli.memoize import memoized
from kroger_cli import helper
from pyppeteer import launch


class KrogerAPI:
    browser_options = {
        'headless': True,
        'userDataDir': '.user-data',
        'args': ['--no-sandbox',
                 '--disable-dev-shm-usage',
                 '--disable-blink-features=AutomationControlled',  # Hide automation detection
                 '--exclude-switches=enable-automation',  # Remove automation switches
                 '--disable-extensions-except',  # Disable extension detection
                 '--disable-plugins-discovery',  # Disable plugin discovery
                 '--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36']
    }
    headers = {
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/81.0.4044.129 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9'
    }

    def __init__(self, cli):
        self.cli: kroger_cli.cli.KrogerCLI = cli

    def complete_survey(self):
        # Cannot use headless mode here for some reason (sign-in cookie doesn't stick)
        self.browser_options['headless'] = False
        res = asyncio.run(self._complete_survey())
        self.browser_options['headless'] = True

        return res

    @memoized
    def get_account_info(self):
        return asyncio.run(self._get_account_info())

    @memoized
    def get_points_balance(self):
        return asyncio.run(self._get_points_balance())

    def clip_coupons(self):
        # Force non-headless mode for clip_coupons since Azure AD B2C has issues in headless mode
        original_headless = self.browser_options['headless']
        self.browser_options['headless'] = False
        try:
            result = asyncio.run(self._clip_coupons())
        finally:
            self.browser_options['headless'] = original_headless
        return result

    @memoized
    def get_purchases_summary(self):
        return asyncio.run(self._get_purchases_summary())

    async def _retrieve_feedback_url(self):
        self.cli.console.print('Loading `My Purchases` page (to retrieve the Feedback’s Entry ID)')

        # Model overlay pop up (might not exist)
        # Need to click on it, as it prevents me from clicking on `Order Details` link
        try:
            await self.page.waitForSelector('.ModalitySelectorDynamicTooltip--Overlay', {'timeout': 10000})
            await self.page.click('.ModalitySelectorDynamicTooltip--Overlay')
        except Exception:
            pass

        try:
            # `See Order Details` link
            await self.page.waitForSelector('.PurchaseCard-top-view-details-button', {'timeout': 10000})
            await self.page.click('.PurchaseCard-top-view-details-button a')
            # `View Receipt` link
            await self.page.waitForSelector('.PurchaseCard-top-view-details-button a', {'timeout': 10000})
            await self.page.click('.PurchaseCard-top-view-details-button a')
            content = await self.page.content()
        except Exception:
            link = 'https://www.' + self.cli.config['main']['domain'] + '/mypurchases'
            self.cli.console.print('[bold red]Couldn’t retrieve the latest purchase, please make sure it exists: '
                                   '[link=' + link + ']' + link + '[/link][/bold red]')
            raise Exception

        try:
            match = re.search('Entry ID: (.*?) ', content)
            entry_id = match[1]
            match = re.search('Date: (.*?) ', content)
            entry_date = match[1]
            match = re.search('Time: (.*?) ', content)
            entry_time = match[1]
            self.cli.console.print('Entry ID retrieved: ' + entry_id)
        except Exception:
            self.cli.console.print('[bold red]Couldn’t retrieve Entry ID from the receipt, please make sure it exists: '
                                   '[link=' + self.page.url + ']' + self.page.url + '[/link][/bold red]')
            raise Exception

        entry = entry_id.split('-')
        hour = entry_time[0:2]
        minute = entry_time[3:5]
        meridian = entry_time[5:7].upper()
        date = datetime.datetime.strptime(entry_date, '%m/%d/%y')
        full_date = date.strftime('%m/%d/%Y')
        month = date.strftime('%m')
        day = date.strftime('%d')
        year = date.strftime('%Y')

        url = f'https://www.krogerstoresfeedback.com/Index.aspx?' \
              f'CN1={entry[0]}&CN2={entry[1]}&CN3={entry[2]}&CN4={entry[3]}&CN5={entry[4]}&CN6={entry[5]}&' \
              f'Index_VisitDateDatePicker={month}%2f{day}%2f{year}&' \
              f'InputHour={hour}&InputMeridian={meridian}&InputMinute={minute}'

        return url, full_date

    async def _complete_survey(self):
        signed_in = await self.sign_in_routine(redirect_url='/mypurchases', contains=['My Purchases'])
        if not signed_in:
            await self.destroy()
            return None

        try:
            url, survey_date = await self._retrieve_feedback_url()
        except Exception:
            await self.destroy()
            return None

        await self.page.goto(url)
        await self.page.waitForSelector('#Index_VisitDateDatePicker', {'timeout': 10000})
        # We need to manually set the date, otherwise the validation fails
        js = "() => {$('#Index_VisitDateDatePicker').datepicker('setDate', '" + survey_date + "');}"
        await self.page.evaluate(js)
        await self.page.click('#NextButton')

        for i in range(35):
            current_url = self.page.url
            try:
                await self.page.waitForSelector('#NextButton', {'timeout': 5000})
            except Exception:
                if 'Finish' in current_url:
                    await self.destroy()
                    return True
            await self.page.evaluate(helper.get_survey_injection_js(self.cli.config))
            await self.page.click('#NextButton')

        await self.destroy()
        return False

    async def _get_account_info(self):
        # First try to get basic info from dashboard, then detailed info from profile page
        signed_in = await self.sign_in_routine(redirect_url='/account/dashboard/', contains=['dashboard', 'account'])
        if not signed_in:
            self.cli.console.print('[red]Authentication failed, returning empty account info[/red]')
            await self.destroy()
            return {
                'firstName': '',
                'lastName': '',
                'emailAddress': '',
                'loyaltyCardNumber': '',
                'mobilePhoneNumber': '',
                'address': {
                    'addressLine1': '',
                    'addressLine2': '',
                    'city': '',
                    'state': '',
                    'zipCode': ''
                }
            }

        self.cli.console.print('Loading profile info from dashboard and profile pages..')
        
        # Wait for page to fully load
        await self.page.waitFor(3000)
        
        try:
            # Get basic info from dashboard (like the welcome message)
            dashboard_info = await self.page.evaluate('''
                () => {
                    const info = {};
                    
                    // Look for welcome message with name - specifically "Welcome, BRETT"
                    const allText = document.body.textContent;
                    const welcomePatterns = [
                        /Welcome,\\s+([A-Z]+)/i,
                        /Hello,\\s+([A-Z]+)/i,
                        /Hi,\\s+([A-Z]+)/i
                    ];
                    
                    for (const pattern of welcomePatterns) {
                        const match = allText.match(pattern);
                        if (match) {
                            info.firstName = match[1].trim();
                            break;
                        }
                    }
                    
                    return info;
                }
            ''')
            
            self.cli.console.print(f'[blue]Dashboard info found: {dashboard_info}[/blue]')
            
            # Now navigate to the profile page for detailed information
            profile_url = 'https://www.' + self.cli.config['main']['domain'] + '/account/update'
            self.cli.console.print(f'[blue]Navigating to profile page: {profile_url}[/blue]')
            
            await self.page.goto(profile_url)
            await self.page.waitFor(3000)  # Wait for profile page to load
            
            # Extract detailed info from profile page
            profile_info = await self.page.evaluate('''
                () => {
                    const info = {};
                    
                    // Look for first name input
                    const firstNameInput = document.querySelector('input[data-qa="Name-firstNameInput"]') || 
                                         document.querySelector('input[name="firstName"]');
                    if (firstNameInput) {
                        info.firstName = firstNameInput.value;
                    }
                    
                    // Look for last name input
                    const lastNameInput = document.querySelector('input[data-qa="Name-lastNameInput"]') || 
                                        document.querySelector('input[name="lastName"]');
                    if (lastNameInput) {
                        info.lastName = lastNameInput.value;
                    }
                    
                    // Look for email address span
                    const emailSpan = document.querySelector('span[data-qa*="Current Email"]');
                    if (emailSpan) {
                        info.email = emailSpan.textContent.trim();
                    }
                    
                    // Look for loyalty card number span
                    const loyaltySpan = document.querySelector('span[data-qa*="Current Plus Card Number"]');
                    if (loyaltySpan) {
                        info.loyaltyCard = loyaltySpan.textContent.trim();
                    }
                    
                    // Look for Alt ID span
                    const altIdSpan = document.querySelector('span[data-qa*="Current Alt ID"]');
                    if (altIdSpan) {
                        info.altId = altIdSpan.textContent.trim();
                    }
                    
                    // Look for phone number input
                    const phoneInput = document.querySelector('input[data-qa="HomePhone-input"]') || 
                                     document.querySelector('input[name="homePhone"]');
                    if (phoneInput && phoneInput.value) {
                        info.phone = phoneInput.value;
                    }
                    
                    return info;
                }
            ''')
            
            self.cli.console.print(f'[blue]Profile page info found: {profile_info}[/blue]')
            
            # Combine dashboard and profile info, prefer profile page data for names
            account_info = {
                'firstName': profile_info.get('firstName') or dashboard_info.get('firstName') or '',
                'lastName': profile_info.get('lastName') or '',
                'emailAddress': profile_info.get('email') or '',
                'loyaltyCardNumber': profile_info.get('loyaltyCard') or '',
                'mobilePhoneNumber': profile_info.get('phone') or '',
                'address': {
                    'addressLine1': '',
                    'addressLine2': '',
                    'city': '',
                    'state': '',
                    'zipCode': ''
                }
            }
            
            # Add Alt ID to the display if available
            if profile_info.get('altId'):
                account_info['altId'] = profile_info['altId']
            
            self.cli.console.print(f'[green]Combined account info: {account_info}[/green]')
            return account_info
                
        except Exception as e:
            self.cli.console.print(f'[red]Error loading account info: {str(e)}[/red]')
            # Return empty structure to prevent KeyError
            return {
                'firstName': '',
                'lastName': '',
                'emailAddress': '',
                'loyaltyCardNumber': '',
                'mobilePhoneNumber': '',
                'address': {
                    'addressLine1': '',
                    'addressLine2': '',
                    'city': '',
                    'state': '',
                    'zipCode': ''
                }
            }
        finally:
            await self.destroy()

    async def _get_points_balance(self):
        signed_in = await self.sign_in_routine()
        if not signed_in:
            await self.destroy()
            return None

        self.cli.console.print('Loading points balance..')
        await self.page.goto('https://www.' + self.cli.config['main']['domain'] + '/accountmanagement/api/points-summary')
        try:
            content = await self.page.content()
            balance = self._get_json_from_page_content(content)
            program_balance = balance[0]['programBalance']['balance']
        except Exception:
            balance = None
        await self.destroy()

        return balance

    async def _clip_coupons(self):
        # Use a more generic check for the coupons page
        signed_in = await self.sign_in_routine(redirect_url='/savings/cl/coupons/', contains=['coupon'])
        if not signed_in:
            await self.destroy()
            return None

        # Wait for page to fully load
        await self.page.waitFor(3000)

        self.cli.console.print('[italic]Searching for available coupons to clip...[/italic]')
        
        try:
            # First, scroll through the entire page to load all lazy-loaded coupons
            self.cli.console.print('[blue]Scrolling through page to load all coupons...[/blue]')
            
            # Get initial page height
            last_height = await self.page.evaluate('document.body.scrollHeight')
            
            while True:
                # Scroll to bottom
                await self.page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                
                # Wait for new content to load
                await self.page.waitFor(750)  # Wait for lazy loading
                
                # Calculate new scroll height and compare with last scroll height
                new_height = await self.page.evaluate('document.body.scrollHeight')
                
                if new_height == last_height:
                    break  # No more content to load
                    
                last_height = new_height
                self.cli.console.print('[blue]Loading more coupons...[/blue]')
            
            self.cli.console.print('[blue]Finished loading all coupons, starting to clip...[/blue]')
            
            # Now find all coupon buttons
            coupon_buttons = await self.page.querySelectorAll('button[data-testid^="CouponActionButton-"]')
            self.cli.console.print(f'[blue]Found {len(coupon_buttons)} total coupon buttons[/blue]')
            
            # First pass: collect all clippable buttons and their test IDs
            clippable_buttons = []
            
            for i, button in enumerate(coupon_buttons):
                try:
                    # Get button text to check if it's clippable
                    button_text = await self.page.evaluate('(element) => element.textContent.trim()', button)
                    
                    if button_text.lower().strip() == "clip":
                        # Get the data-testid for later verification
                        testid = await self.page.evaluate('(element) => element.getAttribute("data-testid")', button)
                        clippable_buttons.append((button, testid, i+1))
                        print(f"Found clippable coupon {i+1}")
                    else:
                        print(f"Skipping coupon {i+1} (text: '{button_text}')")
                        
                except Exception as e:
                    print(f"✗ Error checking coupon {i+1}: {e}")
            
            if not clippable_buttons:
                self.cli.console.print('[yellow]No coupons to clip on this page[/yellow]')
                clipped_count = 0
            else:
                self.cli.console.print(f'[blue]Clicking {len(clippable_buttons)} coupons rapidly...[/blue]')
                
                # Second pass: click all clippable buttons rapidly
                clicked_testids = []
                for button, testid, coupon_num in clippable_buttons:
                    try:
                        await button.click()
                        clicked_testids.append((testid, coupon_num))
                        print(f"Clicked coupon {coupon_num}")
                        # Small delay to avoid overwhelming the server
                        await asyncio.sleep(0.1)
                    except Exception as e:
                        print(f"✗ Error clicking coupon {coupon_num}: {e}")
                
                # Third pass: wait a bit and then verify all clicks
                self.cli.console.print('[blue]Waiting for coupons to update...[/blue]')
                await asyncio.sleep(3)  # Give time for all the async updates to complete
                
                clipped_count = 0
                self.cli.console.print('[blue]Verifying coupon clips...[/blue]')
                
                for testid, coupon_num in clicked_testids:
                    try:
                        # Find the button again to check its updated state
                        updated_button = await self.page.querySelector(f'button[data-testid="{testid}"]')
                        
                        if updated_button:
                            updated_text = await self.page.evaluate('(element) => element.textContent.trim()', updated_button)
                            if updated_text.lower().strip() == "unclip":
                                print(f"✓ Successfully clipped coupon {coupon_num}")
                                clipped_count += 1
                            else:
                                print(f"✗ Failed to clip coupon {coupon_num} (text: {updated_text})")
                        else:
                            # Button not found - might have been removed, assume success
                            print(f"✓ Coupon {coupon_num} likely clipped (button no longer found)")
                            clipped_count += 1
                            
                    except Exception as verify_error:
                        print(f"✗ Could not verify coupon {coupon_num}: {verify_error}")
                
                self.cli.console.print(f'[blue]Successfully clipped {clipped_count} out of {len(clicked_testids)} attempted coupons[/blue]')
            
            failed_count = 0
                        
        except Exception as e:
            self.cli.console.print(f'[red]Error during coupon clipping: {str(e)}[/red]')
            clipped_count = 0
        
        if clipped_count > 0:
            self.cli.console.print(f'[bold green]Successfully clipped {clipped_count} coupons to your account! 👍[/bold green]')
        else:
            self.cli.console.print('[yellow]No new coupons found to clip. All available coupons may already be clipped.[/yellow]')
                
        await self.destroy()
        return clipped_count

    async def _get_purchases_summary(self):
        signed_in = await self.sign_in_routine()
        if not signed_in:
            await self.destroy()
            return None

        self.cli.console.print('Loading your purchases..')
        await self.page.goto('https://www.' + self.cli.config['main']['domain'] + '/mypurchases/api/v1/receipt/summary/by-user-id')
        try:
            content = await self.page.content()
            data = self._get_json_from_page_content(content)
        except Exception:
            data = None
        await self.destroy()

        return data

    async def init(self):
        self.browser = await launch(self.browser_options)
        self.page = await self.browser.newPage()
        
        # Hide automation indicators
        await self.page.evaluateOnNewDocument('''
            () => {
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                });
                
                // Remove automation detection
                delete navigator.__proto__.webdriver;
                
                // Mock plugins
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5],
                });
                
                // Mock languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                });
            }
        ''')
        
        await self.page.setExtraHTTPHeaders(self.headers)
        await self.page.setViewport({'width': 1366, 'height': 768})  # Common screen resolution

    async def destroy(self):
        await self.page.close()
        await self.browser.close()

    async def sign_in_routine(self, redirect_url='/account/update', contains=None):
        if contains is None and redirect_url == '/account/update':
            contains = ['Profile Information']

        await self.init()
        
        # First, check if we're already authenticated by trying to access the target page directly
        self.cli.console.print('[italic]Checking for existing authentication...[/italic]')
        target_url = 'https://www.' + self.cli.config['main']['domain'] + redirect_url
        
        try:
            # Try to navigate directly to the target page
            await self.page.goto(target_url, {'timeout': 10000})
            await asyncio.sleep(2)  # Give page time to load
            
            current_url = self.page.url
            page_content = await self.page.content()
            
            # Check if we're actually on the target page (not redirected to login)
            if current_url.startswith(target_url) or (contains and any(term in page_content for term in contains)):
                self.cli.console.print('[green]Already authenticated! Skipping login process.[/green]')
                return True
            elif 'signin' in current_url.lower() or 'login' in current_url.lower() or 'auth' in current_url.lower():
                self.cli.console.print('[yellow]Not authenticated, proceeding with login...[/yellow]')
            else:
                self.cli.console.print(f'[yellow]Unexpected redirect to: {current_url}, will try authentication...[/yellow]')
                
        except Exception as e:
            self.cli.console.print(f'[yellow]Could not check existing authentication: {str(e)}, proceeding with login...[/yellow]')
        
        # If not already authenticated, proceed with normal login
        self.cli.console.print('[italic]Signing in.. (please wait, it might take awhile)[/italic]')
        signed_in = await self.sign_in(redirect_url, contains)

        if not signed_in and self.browser_options['headless']:
            self.cli.console.print('[red]Sign in failed. Trying one more time..[/red]')
            self.browser_options['headless'] = False
            await self.destroy()
            await self.init()
            signed_in = await self.sign_in(redirect_url, contains)

        if not signed_in:
            self.cli.console.print('[bold red]Sign in failed. Please make sure the username/password is correct.'
                                   '[/bold red]')

        return signed_in

    async def sign_in(self, redirect_url, contains):
        timeout = 30000  # Increased timeout for complex auth flows
        if not self.browser_options['headless']:
            timeout = 60000
        
        signin_url = 'https://www.' + self.cli.config['main']['domain'] + '/signin?redirectUrl=' + redirect_url
        
        # Try to navigate to the signin page with retry logic
        max_retries = 2  # Reduced retries since we already tried direct access
        for attempt in range(max_retries):
            try:
                self.cli.console.print(f'[blue]Loading signin page: {signin_url}[/blue]')
                
                # Use very basic navigation with shorter timeout to avoid infinite hangs
                await self.page.goto(signin_url, {'timeout': 20000})
                
                # Wait a bit for any redirects or dynamic content
                self.cli.console.print('[blue]Waiting for page to fully load...[/blue]')
                await asyncio.sleep(3)
                
                current_url = self.page.url
                self.cli.console.print(f'[blue]Current URL after navigation: {current_url}[/blue]')
                
                # Check if page loaded successfully
                try:
                    page_title = await self.page.title()
                    self.cli.console.print(f'[blue]Page title: {page_title}[/blue]')
                    
                    if page_title:
                        if 'sign' in page_title.lower() or 'login' in page_title.lower():
                            self.cli.console.print('[green]Authentication page loaded successfully[/green]')
                            break
                        elif 'kroger' in page_title.lower():
                            self.cli.console.print('[green]Kroger page loaded[/green]')
                            break
                        else:
                            self.cli.console.print(f'[yellow]Unexpected page loaded: {page_title}[/yellow]')
                    else:
                        self.cli.console.print('[yellow]Could not get page title, but page seems loaded[/yellow]')
                        break
                        
                except Exception as title_e:
                    self.cli.console.print(f'[yellow]Could not get page title: {str(title_e)}[/yellow]')
                    # Continue anyway - page might still be functional
                    break
                    
            except Exception as e:
                if attempt == max_retries - 1:  # Last attempt
                    self.cli.console.print(f'[bold red]Failed to load signin page after {max_retries} attempts: {str(e)}[/bold red]')
                    
                    # Check if we're in headless mode and this might be the issue
                    if self.browser_options['headless']:
                        self.cli.console.print('[red]Azure AD B2C authentication may not work in headless mode[/red]')
                        self.cli.console.print('[yellow]Consider running with browser visible or check network connectivity[/yellow]')
                    
                    return False
                else:
                    self.cli.console.print(f'[yellow]Attempt {attempt + 1} failed, retrying... ({str(e)})[/yellow]')
                    await asyncio.sleep(3)  # Wait before retry
        
        try:
            self.cli.console.print('[blue]Looking for email input field...[/blue]')
            
            # First, let's see what's actually on the page
            page_title = await self.page.title()
            self.cli.console.print(f'[blue]Page title: {page_title}[/blue]')
            
            # Try to find email input with different possible selectors
            # Updated for Azure AD B2C and modern Kroger login
            email_selectors = [
                '#signInName',  # Original Kroger selector
                '#email',  # Common Azure AD B2C selector
                'input[name="signInName"]',  # Name attribute
                'input[name="email"]',
                'input[name="username"]',
                'input[type="email"]',
                'input[placeholder*="email"]',
                'input[placeholder*="Email"]',
                'input[id*="email"]',
                'input[id*="Email"]',
                'input[class*="email"]',
                '[data-testid*="email"]'
            ]
            
            email_selector = None
            for selector in email_selectors:
                try:
                    await self.page.waitForSelector(selector, {'timeout': 2000})
                    email_selector = selector
                    self.cli.console.print(f'[green]Found email field with selector: {selector}[/green]')
                    break
                except Exception:
                    continue
            
            if not email_selector:
                self.cli.console.print('[red]No email field found[/red]')
                return False
            
            await self.page.click(email_selector, {'clickCount': 3})  # Select all in the field
            await self.page.type(email_selector, self.cli.username)
            self.cli.console.print('[green]Email entered successfully[/green]')
            
            self.cli.console.print('[blue]Looking for password input field...[/blue]')
            
            # Try to find password input with different possible selectors
            # Updated for Azure AD B2C and modern Kroger login
            password_selectors = [
                '#password',  # Current correct selector you found
                '#signInPassword',  # Possible Kroger selector
                'input[name="password"]',  # Name attribute
                'input[type="password"]',
                'input[placeholder*="password"]',
                'input[placeholder*="Password"]',
                'input[id*="password"]',
                'input[id*="Password"]',
                'input[class*="password"]',
                '[data-testid*="password"]'
            ]
            
            password_selector = None
            for selector in password_selectors:
                try:
                    await self.page.waitForSelector(selector, {'timeout': 2000})
                    password_selector = selector
                    self.cli.console.print(f'[green]Found password field with selector: {selector}[/green]')
                    break
                except Exception:
                    continue
            
            if not password_selector:
                self.cli.console.print('[red]No password field found[/red]')
                return False
            
            await self.page.click(password_selector, {'clickCount': 3})
            await self.page.type(password_selector, self.cli.password)
            self.cli.console.print('[green]Password entered successfully[/green]')
            
            self.cli.console.print('[blue]Submitting login form...[/blue]')
            
            # Try multiple ways to submit the form
            try:
                # First try: Look for and click the submit button
                submit_selectors = [
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button[id*="submit"]',
                    'button[id*="signin"]',
                    'button[id*="login"]',
                    '.btn-submit',
                    '.submit-button'
                ]
                
                submit_clicked = False
                for selector in submit_selectors:
                    try:
                        await self.page.waitForSelector(selector, {'timeout': 3000})
                        await self.page.click(selector)
                        self.cli.console.print(f'[green]Clicked submit button: {selector}[/green]')
                        submit_clicked = True
                        break
                    except Exception:
                        continue
                
                if not submit_clicked:
                    # Fallback: Press Enter on password field
                    self.cli.console.print('[yellow]No submit button found, pressing Enter[/yellow]')
                    await self.page.focus(password_selector)
                    await self.page.keyboard.press('Enter')
                
            except Exception as e:
                self.cli.console.print(f'[yellow]Submit attempt failed, trying Enter key: {str(e)}[/yellow]')
                await self.page.keyboard.press('Enter')
            
            self.cli.console.print('[blue]Waiting for navigation after login...[/blue]')
            await self.page.waitForNavigation({'timeout': timeout})
            self.cli.console.print('[green]Navigation completed[/green]')
            
        except Exception as e:
            self.cli.console.print(f'[bold red]Error during login process: {str(e)}[/bold red]')
            return False

        if contains is not None:
            self.cli.console.print(f'[blue]Checking page content for: {contains}[/blue]')
            html = await self.page.content()
            for item in contains:
                if item not in html:
                    self.cli.console.print(f'[red]Expected content "{item}" not found on page[/red]')
                    return False
            self.cli.console.print('[green]All expected content found[/green]')

        return True

    def _get_json_from_page_content(self, content):
        match = re.search('<pre.*?>(.*?)</pre>', content)
        return json.loads(match[1])
