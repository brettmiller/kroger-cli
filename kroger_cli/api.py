import asyncio
import json
import re
import datetime
import kroger_cli.cli
from kroger_cli.memoize import memoized
from kroger_cli import helper
from playwright.async_api import async_playwright, BrowserContext, Page


class KrogerAPI:
    def __init__(self, cli):
        self.cli: kroger_cli.cli.KrogerCLI = cli
        # Initialize browser and page attributes with proper types
        self.browser: BrowserContext | None = None
        self.page: Page | None = None
        self.playwright = None
        # Make browser_options an instance variable so it doesn't get shared
        self.browser_options = {
            'headless': True,
            'userDataDir': '.user-data',
            'args': ['--blink-settings=imagesEnabled=false',  # Disable images for hopefully faster load-time
                     '--no-sandbox']
        }
        self.headers = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/81.0.4044.129 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9'
        }

    @memoized
    def get_account_info(self):
        return asyncio.run(self._get_account_info())

    @memoized
    def get_points_balance(self):
        return asyncio.run(self._get_points_balance())

    def clip_coupons(self):
        # Try headless mode first with improved error handling
        return asyncio.run(self._clip_coupons())

    async def _get_account_info(self):
        self.cli.console.print('Loading `My Purchases` page (to retrieve the Feedback’s Entry ID)')

        # Model overlay pop up (might not exist)
        # Need to click on it, as it prevents me from clicking on `Order Details` link
        try:
            await self.page.wait_for_selector('.ModalitySelectorDynamicTooltip--Overlay', timeout=10000)
            await self.page.click('.ModalitySelectorDynamicTooltip--Overlay')
        except Exception:
            pass

        try:
            # `See Order Details` link
            await self.page.wait_for_selector('.PurchaseCard-top-view-details-button', timeout=10000)
            await self.page.click('.PurchaseCard-top-view-details-button a')
            # `View Receipt` link
            await self.page.wait_for_selector('.PurchaseCard-top-view-details-button a', timeout=10000)
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
        await self.page.wait_for_selector('#Index_VisitDateDatePicker', timeout=10000)
        # We need to manually set the date, otherwise the validation fails
        js = "() => {$('#Index_VisitDateDatePicker').datepicker('setDate', '" + survey_date + "');}"
        await self.page.evaluate(js)
        await self.page.click('#NextButton')

        for i in range(35):
            current_url = self.page.url
            try:
                await self.page.wait_for_selector('#NextButton', timeout=5000)
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
        await self.page.wait_for_timeout(3000)
        
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
            await self.page.wait_for_timeout(3000)  # Wait for profile page to load
            
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
        # Use the same pattern as account-info - simple and reliable
        signed_in = await self.sign_in_routine(redirect_url='/points/summary', contains=['points'])
        if not signed_in:
            await self.destroy()
            return None

        self.cli.console.print('Loading points balance..')
        
        # Wait for page to fully load (same as account-info)
        await self.page.wait_for_timeout(3000)
        
        # Navigate directly to points page (same pattern as account-info)
        target_url = 'https://www.' + self.cli.config['main']['domain'] + '/points/summary'
        self.cli.console.print(f'[blue]Navigating to points page: {target_url}[/blue]')
        
        await self.page.goto(target_url)
        await self.page.wait_for_timeout(5000)  # Wait for points page to load
        
        # Debug: Take a screenshot and save HTML for inspection
        try:
            await self.page.screenshot(path="debug_points_page.png", full_page=True)
            html_content = await self.page.content()
            with open("debug_points_page.html", "w") as f:
                f.write(html_content)
            self.cli.console.print("[dim]Debug: Saved screenshot and HTML content[/dim]")
        except Exception:
            pass
        
        try:
            # Look specifically for points summary cards - be much more selective
            points_info = await self.page.evaluate('''
                () => {
                    const info = {
                        monthlyBalances: [],
                        totalBalance: 0,
                        debugInfo: {
                            foundElements: [],
                            pageText: document.body.textContent.substring(0, 500),
                            pageTitle: document.title,
                            url: window.location.href
                        }
                    };
                    
                    // Strategy 1: Look for specific large points cards (monthly summaries)
                    const largePointsCards = document.querySelectorAll('.PointsLargeCard, [data-testid*="LargePointCard"], [class*="PointsLarge"]');
                    info.debugInfo.foundElements.push(`Found ${largePointsCards.length} large points cards`);
                    
                    for (const card of largePointsCards) {
                        // Look for month element
                        const monthElement = card.querySelector('[data-testid="LargePointCardMonth"]') || 
                                           card.querySelector('[data-testid*="Month"]') ||
                                           card.querySelector('.month, .Month');
                        
                        // Look for points value element  
                        const pointsElement = card.querySelector('.PointsLargeValue') || 
                                            card.querySelector('[data-testid*="Value"]') ||
                                            card.querySelector('.value, .Value') ||
                                            card.querySelector('.font-bold');
                        
                        // Look for expiration element
                        const expirationElement = card.querySelector('[data-testid*="Expiration"]') ||
                                                 card.querySelector('.expiration, .Expiration');
                        
                        if (monthElement && pointsElement) {
                            const month = monthElement.textContent.trim();
                            const pointsText = pointsElement.textContent.trim();
                            const points = parseInt(pointsText.replace(/[^0-9]/g, ''));
                            
                            if (!isNaN(points) && points >= 0) {  // Include 0 points as well
                                let expiration = '';
                                if (expirationElement) {
                                    expiration = expirationElement.textContent.trim();
                                }
                                
                                info.monthlyBalances.push({
                                    month: month,
                                    points: points,
                                    expiration: expiration
                                });
                                info.totalBalance += points;
                            }
                        }
                    }
                    
                    // Strategy 2: If no large cards found, look for summary sections
                    if (info.monthlyBalances.length === 0) {
                        const summaryElements = document.querySelectorAll('.points-summary, .PointsSummary, [class*="summary"], [class*="Summary"]');
                        info.debugInfo.foundElements.push(`Found ${summaryElements.length} summary elements`);
                        
                        for (const section of summaryElements) {
                            const text = section.textContent;
                            // Look for patterns like "November: 1000 points" or "Current: 500 points"
                            const monthPattern = /(January|February|March|April|May|June|July|August|September|October|November|December|Current|Total)[:\\s]*(\\d{1,6})\\s*points?/gi;
                            const matches = [...text.matchAll(monthPattern)];
                            
                            for (const match of matches) {
                                const month = match[1];
                                const points = parseInt(match[2]);
                                
                                if (points > 0) {
                                    info.monthlyBalances.push({
                                        month: month,
                                        points: points,
                                        expiration: ''
                                    });
                                    info.totalBalance += points;
                                }
                            }
                        }
                    }
                    
                    // Strategy 3: Look for balance-specific content
                    if (info.monthlyBalances.length === 0) {
                        const allText = document.body.textContent;
                        // Only look for very specific balance patterns
                        const balancePatterns = [
                            /(?:Available|Current|Total)\\s*Balance[:\\s]*(\\d{1,6})\\s*points?/gi,
                            /(?:November|December|Current)\\s*(?:Points|Balance)[:\\s]*(\\d{1,6})/gi
                        ];
                        
                        for (const pattern of balancePatterns) {
                            const matches = [...allText.matchAll(pattern)];
                            if (matches.length > 0 && matches.length <= 3) { // Only if reasonable number of matches
                                for (const match of matches) {
                                    const points = parseInt(match[1]);
                                    if (points > 0) {
                                        info.monthlyBalances.push({
                                            month: 'Current',
                                            points: points,
                                            expiration: ''
                                        });
                                        info.totalBalance += points;
                                    }
                                }
                                break; // Take first working pattern
                            }
                        }
                    }
                    
                    return info;
                }
            ''')
            
            # Log debug information
            if points_info.get('debugInfo'):
                self.cli.console.print(f'[dim]Page title: {points_info["debugInfo"].get("pageTitle", "N/A")}[/dim]')
                self.cli.console.print(f'[dim]Current URL: {points_info["debugInfo"].get("url", "N/A")}[/dim]')
                for msg in points_info['debugInfo']['foundElements']:
                    self.cli.console.print(f'[dim]{msg}[/dim]')
                if not points_info.get('monthlyBalances'):
                    self.cli.console.print(f'[dim]Page text sample: {points_info["debugInfo"]["pageText"][:200]}...[/dim]')
            
            self.cli.console.print(f'[green]Found {len(points_info.get("monthlyBalances", []))} monthly balance(s), total: {points_info.get("totalBalance", 0)} points[/green]')
            
            if points_info.get('monthlyBalances') and len(points_info['monthlyBalances']) > 0:
                # Format the response to match the expected structure
                # The CLI expects balance[0] to be metadata and balance[1+] to be actual programs
                balance = [
                    {
                        # Metadata entry (index 0)
                        'metadata': 'success'
                    }
                ]
                
                # Add each monthly balance as a separate program entry
                for monthly_balance in points_info['monthlyBalances']:
                    expiration_text = monthly_balance.get('expiration', '')
                    if expiration_text:
                        description = f"{monthly_balance['points']} points ({expiration_text})"
                    else:
                        description = f"{monthly_balance['points']} points"
                    
                    balance.append({
                        'programDisplayInfo': {
                            'loyaltyProgramName': f"Kroger Fuel Points ({monthly_balance['month']})"
                        },
                        'programBalance': {
                            'balance': monthly_balance['points'],
                            'balanceDescription': description
                        }
                    })
                
                # Also add a total if there are multiple months
                if len(points_info['monthlyBalances']) > 1:
                    balance.append({
                        'programDisplayInfo': {
                            'loyaltyProgramName': 'Kroger Fuel Points (Total)'
                        },
                        'programBalance': {
                            'balance': points_info['totalBalance'],
                            'balanceDescription': f"{points_info['totalBalance']} points total"
                        }
                    })
            else:
                # Fallback: try the old JSON method in case the page still has it
                self.cli.console.print('[yellow]No monthly points found in HTML, trying JSON fallback...[/yellow]')
                content = await self.page.content()
                balance = self._get_json_from_page_content(content)
                
        except Exception as e:
            self.cli.console.print(f'[red]Error extracting points balance: {str(e)}[/red]')
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
        await self.page.wait_for_timeout(3000)
        
        # Check if we actually got to a coupons page by looking for coupon-related elements
        try:
            page_content = await self.page.content()
            current_url = self.page.url
            
            # If we have coupon content or are on the right URL, proceed
            if ('coupon' in page_content.lower() or 
                'savings' in current_url.lower() or 
                'clip' in page_content.lower()):
                self.cli.console.print('[green]Successfully accessed coupons page in headless mode[/green]')
            else:
                self.cli.console.print('[yellow]Page content verification failed, but attempting to proceed...[/yellow]')
        except Exception as e:
            self.cli.console.print(f'[yellow]Could not verify page content: {str(e)}, proceeding anyway...[/yellow]')

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
                await self.page.wait_for_timeout(750)  # Wait for lazy loading
                
                # Calculate new scroll height and compare with last scroll height
                new_height = await self.page.evaluate('document.body.scrollHeight')
                
                if new_height == last_height:
                    break  # No more content to load
                    
                last_height = new_height
                self.cli.console.print('[blue]Loading more coupons...[/blue]')
            
            self.cli.console.print('[blue]Finished loading all coupons, starting to clip...[/blue]')
            
            # Now find all coupon buttons
            coupon_buttons = await self.page.query_selector_all('button[data-testid^="CouponActionButton-"]')
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
                max_offers_reached = False
                
                for button, testid, coupon_num in clippable_buttons:
                    try:
                        await button.click()
                        clicked_testids.append((testid, coupon_num))
                        print(f"Clicked coupon {coupon_num}")
                        
                        # Check for maximum offers reached after clicking
                        await asyncio.sleep(0.2)  # Give time for any error messages to appear
                        
                        # Check for banner message at top of page
                        try:
                            page_content = await self.page.content()
                            if ('maximum number of offers' in page_content.lower() or 
                                'reached the maximum' in page_content.lower() or
                                'maximum number of offers clipped' in page_content.lower()):
                                self.cli.console.print('[yellow]Maximum number of offers reached! Stopping coupon clipping.[/yellow]')
                                max_offers_reached = True
                                break
                        except Exception:
                            pass
                        
                        # Also check if the button itself shows an error message
                        try:
                            updated_button = await self.page.query_selector(f'button[data-testid="{testid}"]')
                            if updated_button:
                                button_text = await self.page.evaluate('(element) => element.textContent.trim()', updated_button)
                                if 'maximum' in button_text.lower():
                                    self.cli.console.print('[yellow]Maximum number of offers reached! Stopping coupon clipping.[/yellow]')
                                    max_offers_reached = True
                                    break
                        except Exception:
                            pass
                            
                    except Exception as e:
                        error_msg = str(e)
                        # Check if this is a "Node is detached" error, which often indicates DOM changes from limit reached
                        if 'detached from document' in error_msg.lower():
                            # Check if maximum offers message appeared
                            try:
                                page_content = await self.page.content()
                                if ('maximum number of offers' in page_content.lower() or 
                                    'reached the maximum' in page_content.lower()):
                                    self.cli.console.print('[yellow]Maximum number of offers reached! Stopping coupon clipping.[/yellow]')
                                    max_offers_reached = True
                                    break
                            except Exception:
                                pass
                        
                        print(f"✗ Error clicking coupon {coupon_num}: {e}")
                
                if max_offers_reached:
                    self.cli.console.print('[blue]Stopped clipping due to maximum offers limit[/blue]')
                
                # Third pass: wait a bit and then verify all clicks
                self.cli.console.print('[blue]Waiting for coupons to update...[/blue]')
                await asyncio.sleep(3)  # Give time for all the async updates to complete
                
                clipped_count = 0
                self.cli.console.print('[blue]Verifying coupon clips...[/blue]')
                
                for testid, coupon_num in clicked_testids:
                    try:
                        # Find the button again to check its updated state
                        updated_button = await self.page.query_selector(f'button[data-testid="{testid}"]')
                        
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

    async def init(self, headless=True):
        """Initialize browser with headless-first approach, fallback to visible if needed"""
        self.playwright = await async_playwright().start()
        
        # Browser launch options with headless priority
        browser_args = [
            '--disable-blink-features=AutomationControlled',
            '--disable-features=VizDisplayCompositor',
            '--no-sandbox',
            '--disable-dev-shm-usage'
        ]
        
        try:
            # Go back to persistent context to maintain authentication
            if headless:
                self.cli.console.print("[dim]Trying headless mode first...[/dim]")
                self.browser = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir='.user-data',
                    headless=True,
                    args=browser_args,
                    viewport={'width': 1920, 'height': 1080},
                    extra_http_headers=self.headers
                )
            else:
                # Fallback to visible browser
                self.cli.console.print("[dim]Using visible browser mode...[/dim]")
                self.browser = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir='.user-data',
                    headless=False,
                    args=browser_args,
                    viewport={'width': 1920, 'height': 1080},
                    extra_http_headers=self.headers
                )
            
            self.page = await self.browser.new_page()
            
            # Test JavaScript availability
            js_test = await self.page.evaluate('() => typeof window !== "undefined" && typeof document !== "undefined"')
            self.cli.console.print(f"[dim]JavaScript test result: {js_test}[/dim]")
            
            # Hide automation indicators
            await self.page.add_init_script('''
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
            
        except Exception as e:
            if headless:
                self.cli.console.print(f"[yellow]Headless mode failed: {str(e)}[/yellow]")
                self.cli.console.print("[yellow]Falling back to visible browser...[/yellow]")
                await self.destroy()
                return await self.init(headless=False)
            else:
                raise e

    async def destroy(self):
        try:
            if hasattr(self, 'page') and self.page:
                await self.page.close()
        except Exception:
            pass  # Ignore errors during page cleanup
        
        try:
            if hasattr(self, 'browser') and self.browser:
                await self.browser.close()
        except Exception:
            pass  # Ignore errors during browser cleanup
            
        try:
            if hasattr(self, 'playwright') and self.playwright:
                await self.playwright.stop()
        except Exception:
            pass  # Ignore errors during playwright cleanup

    async def sign_in_routine(self, redirect_url='/account/update', contains=None, headless=True):
        if contains is None and redirect_url == '/account/update':
            contains = ['Profile Information']

        await self.init(headless=headless)
        
        # First, check if we're already authenticated by trying to access the target page directly
        self.cli.console.print('[italic]Checking for existing authentication...[/italic]')
        target_url = 'https://www.' + self.cli.config['main']['domain'] + redirect_url
        
        try:
            # Try to navigate directly to the target page
            await self.page.goto(target_url, timeout=10000)
            await asyncio.sleep(2)  # Give page time to load
            
            current_url = self.page.url
            page_content = await self.page.content()
            
            # Check if we're actually on the target page (not redirected to login)
            # Be more strict about authentication detection
            is_authenticated = (
                current_url.startswith(target_url) and 
                not 'login' in current_url.lower() and 
                not 'oauth' in current_url.lower() and
                not 'signin' in current_url.lower() and
                (contains is None or any(term in page_content for term in contains))
            )
            
            if is_authenticated:
                self.cli.console.print('[green]Already authenticated! Skipping login process.[/green]')
                return True
            else:
                auth_indicators = []
                if 'login' in current_url.lower(): auth_indicators.append('login URL')
                if 'oauth' in current_url.lower(): auth_indicators.append('OAuth URL')
                if 'signin' in current_url.lower(): auth_indicators.append('signin URL')
                if not current_url.startswith(target_url): auth_indicators.append(f'redirected to {current_url[:100]}...')
                
                self.cli.console.print(f'[yellow]Not authenticated ({", ".join(auth_indicators)}), proceeding with login...[/yellow]')
                
        except Exception as e:
            self.cli.console.print(f'[yellow]Could not check existing authentication: {str(e)}, proceeding with login...[/yellow]')
        
        # If not already authenticated, proceed with normal login
        self.cli.console.print('[italic]Signing in.. (please wait, it might take awhile)[/italic]')
        signed_in = await self.sign_in(redirect_url, contains)

        # Only fallback to non-headless if authentication truly failed AND we're in headless mode
        if not signed_in and headless:
            self.cli.console.print('[red]Sign in failed in headless mode. Trying with browser visible..[/red]')
            await self.destroy()
            return await self.sign_in_routine(redirect_url, contains, headless=False)

        if not signed_in:
            self.cli.console.print('[bold red]Sign in failed. Please make sure the username/password is correct.'
                                   '[/bold red]')

        return signed_in

    async def sign_in(self, redirect_url, contains):
        timeout = 30000  # Increased timeout for complex auth flows
        # Note: no access to headless state here since we're using persistent context
        
        signin_url = 'https://www.' + self.cli.config['main']['domain'] + '/signin?redirectUrl=' + redirect_url
        
        # Try to navigate to the signin page with retry logic
        max_retries = 2  # Reduced retries since we already tried direct access
        for attempt in range(max_retries):
            try:
                self.cli.console.print(f'[blue]Loading signin page: {signin_url}[/blue]')
                
                # Use very basic navigation with shorter timeout to avoid infinite hangs
                await self.page.goto(signin_url, timeout=20000)
                
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
                error_msg = str(e)
                if attempt == max_retries - 1:  # Last attempt
                    # For HTTP/2 errors, continue anyway since auth often still works
                    if 'net::ERR_HTTP2_PROTOCOL_ERROR' in error_msg:
                        self.cli.console.print('[yellow]HTTP/2 protocol error, but continuing with authentication...[/yellow]')
                        break
                    else:
                        self.cli.console.print(f'[bold red]Failed to load signin page after {max_retries} attempts: {error_msg}[/bold red]')
                        return False
                else:
                    self.cli.console.print(f'[yellow]Attempt {attempt + 1} failed, retrying... ({error_msg})[/yellow]')
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
                    await self.page.wait_for_selector(selector, timeout=2000)
                    email_selector = selector
                    self.cli.console.print(f'[green]Found email field with selector: {selector}[/green]')
                    break
                except Exception:
                    continue
            
            if not email_selector:
                self.cli.console.print('[red]No email field found[/red]')
                return False
            
            await self.page.click(email_selector, click_count=3)  # Select all in the field
            await self.page.fill(email_selector, self.cli.username)
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
                    await self.page.wait_for_selector(selector, timeout=2000)
                    password_selector = selector
                    self.cli.console.print(f'[green]Found password field with selector: {selector}[/green]')
                    break
                except Exception:
                    continue
            
            if not password_selector:
                self.cli.console.print('[red]No password field found[/red]')
                return False
            
            await self.page.click(password_selector, click_count=3)
            await self.page.fill(password_selector, self.cli.password)
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
                        await self.page.wait_for_selector(selector, timeout=3000)
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
            await self.page.wait_for_load_state("networkidle", timeout=timeout)
            self.cli.console.print('[green]Navigation completed[/green]')
            
        except Exception as e:
            error_msg = str(e)
            # Handle "context destroyed" errors specifically - these are often normal for OAuth flows
            if 'context was destroyed' in error_msg.lower() or 'execution context was destroyed' in error_msg.lower():
                self.cli.console.print('[yellow]Context destroyed during login - this is normal for OAuth redirects[/yellow]')
                # Don't return False - this is often a successful login that just lost context tracking
                # Wait a bit for any navigation to complete
                await asyncio.sleep(3)
            else:
                self.cli.console.print(f'[bold red]Error during login process: {error_msg}[/bold red]')
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
        if match:
            return json.loads(match[1])
        else:
            # Debug: Let's see what selectors and content we have available
            self.cli.console.print('[yellow]No <pre> tag found, debugging page content...[/yellow]')
            
            # Check for common React/JS data patterns
            patterns = [
                r'__INITIAL_STATE__\s*=\s*({.*?});',
                r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
                r'__STATE__\s*=\s*({.*?});',
                r'window\.initialState\s*=\s*({.*?});',
                r'"points":\s*(\d+)',
                r'"balance":\s*(\d+)',
                r'"totalPoints":\s*(\d+)',
            ]
            
            for pattern in patterns:
                match = re.search(pattern, content, re.DOTALL)
                if match:
                    self.cli.console.print(f'[green]Found pattern: {pattern[:50]}...[/green]')
                    try:
                        if 'points' in pattern or 'balance' in pattern:
                            # Simple number extraction
                            points = int(match.group(1))
                            return [{}, {
                                'programDisplayInfo': {'loyaltyProgramName': 'Kroger Fuel Points'},
                                'programBalance': {'balance': points, 'balanceDescription': f'{points} points'}
                            }]
                        else:
                            # Try to parse as JSON
                            return json.loads(match.group(1))
                    except (json.JSONDecodeError, ValueError) as e:
                        self.cli.console.print(f'[yellow]Could not parse JSON from pattern: {e}[/yellow]')
                        continue
            
            # If no patterns work, let's examine the page structure
            self.cli.console.print('[yellow]No JSON patterns found, analyzing page structure...[/yellow]')
            
            # Look for any mentions of points in the content
            if 'points' in content.lower():
                # Find lines containing 'points'
                lines_with_points = [line.strip() for line in content.split('\n') if 'points' in line.lower()]
                self.cli.console.print(f'[blue]Found {len(lines_with_points)} lines mentioning points[/blue]')
                for i, line in enumerate(lines_with_points[:5]):  # Show first 5
                    self.cli.console.print(f'[dim]{i+1}: {line[:100]}...[/dim]')
            
            return None
