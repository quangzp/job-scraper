# PROJECT CONTEXT: ENTERPRISE JOB SCRAPER 

## 1. PROJECT OVERVIEW
This project is an Enterprise-grade Job Aggregator. It scrapes job postings from 5 target domains (TopCV, VietnamWorks, etc.) using a decoupled architecture. It uses Django for the Web Dashboard/Database ORM and Crawlee for Python as the core scraping engine. 

The system follows a strict Producer-Consumer pattern managed via PostgreSQL state flags. All code resides in a single Monorepo folder.

## 2. TECH STACK
- Core Framework: Python 3.10+, Django 5.x
- Scraping Engine: Crawlee for Python
- Database & ORM: PostgreSQL + Django ORM (STRICTLY NO SQLAlchemy)
- Deployment: Docker & Docker Compose
- UI/Dashboard: Django Admin (No React/Vue frontend)

## 3. ARCHITECTURE & DIRECTORY STRUCTURE
The project is a Monorepo. Scraping scripts run as standalone Python processes but are wrapped within the Django environment to utilize Django ORM.

## 4. CORE WORKFLOW & STATE MANAGEMENT
Do not deviate from this 2-Phase workflow:

* PHASE 1: Harvesters (Producers)
  Triggered periodically. Read Keyword from DB. Visit target domains -> crawl pagination -> find Job Detail URLs. Save URLs to JobLink model with status='PENDING'. DO NOT extract details here.

  LINKEDIN EXCEPTION: LinkedIn is allowed to extract job details during harvest from the already-loaded job detail panel after a human-like job-card click. Reopening LinkedIn job URLs in a separate extractor increases authwall/checkpoint risk. This exception applies only to LinkedIn; all other domains must keep the strict URL-only harvester behavior.

* PHASE 2: Extractors (Consumers)
  Run continuously. Fetch batch of PENDING links from DB. 
  CRITICAL: Must use select_for_update(skip_locked=True) to prevent Race Conditions between multiple Docker containers.
  Update status to PROCESSING. Scrape detailed data -> save to JobDetail model -> update JobLink status to SUCCESS (or FAILED if error).

## 5. STRICT CODING RULES FOR AI AGENT
When generating code, modifying existing files, or suggesting architectures, you MUST follow these rules:

1. NO SQLALCHEMY: The project uses Django ORM for everything. Do not import or suggest SQLAlchemy.
2. DJANGO SETUP IN SCRIPTS: Any standalone script in scrapers/ MUST include django.setup() before importing models.
3. NO HARDCODED SELECTORS: NEVER hardcode XPath or CSS Selectors inside Python scripts. Always read them from the per-domain files in scrapers/config/selectors/ via scrapers/config/selector_loader.py.
4. K.I.S.S (Rule-based NLP): Do not suggest heavy AI/LLM models for text extraction unless explicitly asked. Use Regex and string mapping in scrapers/utils/.
5. CONCURRENCY SAFE: When updating database records in extractors, always assume multiple workers are running. Use atomic transactions and row locks.
6. CRAWLEE USAGE: Utilize Crawlee's built-in RequestQueue, ProxyConfiguration, and Router. Do not reinvent the wheel with pure Playwright or standard requests if Crawlee has a built-in feature.
7. LINKEDIN COMBINED FLOW: LinkedIn harvester may write JobDetail and mark JobLink as SUCCESS when detail extraction succeeds. If a LinkedIn URL is found but detail extraction fails, save the JobLink as PENDING for later retry/fallback. Do not downgrade an existing SUCCESS link to PENDING.

## 6. DOCKER EXECUTION COMMANDS (Reference)
- Start Web Admin: python manage.py runserver 0.0.0.0:8000
- Start Harvester: python scrapers/run_worker.py --mode=harvest
- Start Extractor: python scrapers/run_worker.py --mode=extract

## 7. Claude code will review code
