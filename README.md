# 🎯 EchoDesk - Multi-Tenant Customer Support Platfor

Test 2
[![Development Hours](https://img.shields.io/badge/Development%20Hours-135%2B-blue.svg)](./TIME_TRACKING.md)
[![Django](https://img.shields.io/badge/Django-4.2+-green.svg)](https://djangoproject.com/)
[![Next.js](https://img.shields.io/badge/Next.js-15.4.4-black.svg)](https://nextjs.org/)
[![Production](https://img.shields.io/badge/Status-Production%20Ready-success.svg)](https://amanati.echodesk.ge/)

EchoDesk is a comprehensive multi-tenant customer support platform that unifies communication channels including phone calls, social media messaging (Facebook, Instagram, WhatsApp), and provides advanced analytics for business customer service operations.

## 🏗️ Architecture Overview

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Next.js 15    │    │   Django 4.2+    │    │  PostgreSQL     │
│   Frontend      │◄──►│   REST API       │◄──►│  Multi-Tenant   │
│   (TypeScript)  │    │   (Multi-Tenant) │    │  Database       │
└─────────────────┘    └──────────────────┘    └─────────────────┘
         │                        │                       │
         │                        ▼                       │
         │              ┌──────────────────┐              │
         │              │ Social Media APIs │              │
         │              │ • Facebook Graph  │              │
         └──────────────┤ • Instagram API   │──────────────┘
                        │ • WhatsApp Business│
                        └──────────────────┘
```

## ✨ Features

### 🔐 **Multi-Tenant Architecture**

- **Schema Isolation:** Each tenant has isolated database schema
- **Subdomain Routing:** `tenant.echodesk.ge` automatic routing
- **Secure Data Separation:** Complete tenant data isolation

### 📞 **Call Management System**

- **SIP Integration:** Real-time call logging and recording
- **Call Analytics:** Detailed statistics and reporting
- **Event Tracking:** Comprehensive call event logging
- **Recording Management:** Automatic call recording storage

### 💬 **Social Media Integration**

- **Facebook Pages:** OAuth 2.0 authentication and messaging
- **Instagram Business:** Direct message management
- **WhatsApp Business:** API integration for customer support
- **Real-time Webhooks:** Instant message synchronization

### 📊 **Dashboard & Analytics**

- **Unified Inbox:** All communication channels in one place
- **Real-time Updates:** Live message and call notifications
- **Performance Metrics:** Detailed analytics and reporting
- **User Management:** Role-based access control

## 🚀 Quick Start

### **Prerequisites**

- Python 3.11+
- Node.js 18+
- PostgreSQL 14+
- Redis (for caching and sessions)

### **Backend Setup**

```bash
# Clone and setup backend
git clone https://github.com/giorgigordiashvili/echodesk-back.git
cd echodesk-back

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Environment configuration
cp env.example .env
# Edit .env with your configuration

# Database setup
python manage.py migrate_schemas
python manage.py createsuperuser

# Run development server
python manage.py runserver
```

### **Demo tenant (local data for every module)**

```bash
python manage.py seed_demo_tenant            # idempotent: re-running never duplicates rows
python manage.py seed_demo_tenant --reset    # drop the demo schema and seed it again
```

Creates tenant schema `demo` on `demo.api.lvh.me` (set `API_DOMAIN=api.lvh.me`,
`MAIN_DOMAIN=lvh.me`) with every feature enabled and sample data in tickets,
leave, invoices, bookings, e-commerce, social inbox (Facebook / Instagram /
WhatsApp / Telegram / website widget), calls, team chat and notifications.
Social, e-mail and PBX connections are DB rows with fake tokens; nothing calls
an external service.

| Login | Role |
|---|---|
| `admin@demo.ge` | superuser + staff, admin |
| `manager@demo.ge` | manager (leave manager of agent) |
| `agent@demo.ge` | agent |
| `staff1@demo.ge` … `staff6@demo.ge` | extra team members |

Password for every demo user: **`Demo12345!`**. The command refuses to touch a
tenant it did not create.

### **Frontend Setup**

```bash
# Setup frontend
cd echodesk-frontend

# Install dependencies
npm install

# Environment configuration
cp .env.example .env.local
# Edit .env.local with your configuration

# Run development server
npm run dev
```

## 📁 Project Structure

```
echodesk-back/
├── amanati_crm/          # Django project configuration
├── tenants/              # Multi-tenant models and logic
├── users/                # User authentication system
├── crm/                  # Core CRM functionality
├── social_integrations/  # Social media API integrations
├── tickets/              # Ticket management system
├── echodesk-frontend/    # Next.js frontend application
├── staticfiles/          # Static files for production
├── templates/            # Django templates
├── locale/               # Internationalization files
└── TIME_TRACKING.md      # Development hours tracking
```

## 🔧 Configuration

### **Environment Variables**

```bash
# Database
DATABASE_URL=postgresql://user:pass@localhost/echodesk

# Social Media APIs
FACEBOOK_APP_ID=your_facebook_app_id
FACEBOOK_APP_SECRET=your_facebook_app_secret
FACEBOOK_API_VERSION=v23.0

INSTAGRAM_APP_ID=your_instagram_app_id
INSTAGRAM_APP_SECRET=your_instagram_app_secret

WHATSAPP_BUSINESS_ACCOUNT_ID=your_whatsapp_account_id
WHATSAPP_ACCESS_TOKEN=your_whatsapp_token

# Security
SECRET_KEY=your_django_secret_key
ALLOWED_HOSTS=localhost,127.0.0.1,.echodesk.ge

# Redis
REDIS_URL=redis://localhost:6379/0
```

## 📡 API Documentation

### **Authentication Endpoints**

- `POST /api/auth/login/` - User login
- `POST /api/auth/logout/` - User logout
- `GET /api/auth/user/` - Current user info

### **Social Media Endpoints**

- `GET /api/social/facebook/pages/` - List connected Facebook pages
- `POST /api/social/facebook/send-message/` - Send Facebook message
- `GET /api/social/instagram/accounts/` - List Instagram accounts
- `POST /api/social/instagram/send-message/` - Send Instagram message

### **Call Management Endpoints**

- `GET /api/crm/calls/` - List call logs
- `POST /api/crm/calls/` - Create call log
- `GET /api/crm/calls/{id}/` - Call details
- `GET /api/crm/call-statistics/` - Call analytics

## 🔗 Integration Guides

- **[Facebook Setup Guide](./FACEBOOK_SETUP_GUIDE.md)** - Facebook Pages integration
- **[Instagram Setup Guide](./INSTAGRAM_SETUP_GUIDE.md)** - Instagram Business setup
- **[WhatsApp Setup Guide](./WHATSAPP_SETUP_GUIDE.md)** - WhatsApp Business API
- **[Call Logging API](./CALL_LOGGING_API.md)** - SIP integration documentation

## 🚀 Deployment

### **Production Deployment (DigitalOcean)**

```bash
# Build frontend
cd echodesk-frontend
npm run build

# Deploy backend
cd ..
./build_production.sh

# Database migrations
python manage.py migrate_schemas --shared
python manage.py migrate_schemas

# Static files
python manage.py collectstatic --noinput
```

### **Environment Setup**

- **Domain:** `echodesk.ge` with wildcard SSL
- **Database:** PostgreSQL with connection pooling
- **File Storage:** DigitalOcean Spaces or AWS S3
- **Caching:** Redis for session and cache storage

## 📊 Development Progress

**Total Development Time:** [135+ hours](./TIME_TRACKING.md)

### **Completed Milestones:**

- ✅ Multi-tenant architecture implementation
- ✅ Social media integrations (Facebook, Instagram, WhatsApp)
- ✅ Call logging and SIP integration
- ✅ Frontend dashboard and user interface
- ✅ Production deployment and optimization
- ✅ OAuth 2.0 flows and webhook processing

### **Current Focus:**

- 🔄 Meta app review submission
- 🔄 Instagram business page setup
- 🔄 Production testing and optimization

## 🤝 Contributing

1. Fork the repository
2. Create feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push to branch (`git push origin feature/amazing-feature`)
5. Open Pull Request

## 📄 License

This project is proprietary software developed for AmanatiLTD. All rights reserved.

## 📞 Support

- **Email:** support@echodesk.ge
- **Website:** [echodesk.ge](https://echodesk.ge)
- **Documentation:** [docs.echodesk.ge](https://docs.echodesk.ge)

---

**Built with ❤️ by the EchoDesk Team**  
*Empowering businesses with unified customer communication*choDesk BackendA

Django-based backend for the EchoDesk CRM system with multi-tenant architecture.

Updated: July 31, 2025 - Enhanced user management system with comprehensive role-based permissions and team management functionality.

## 🏗️ Architecture

- **Main Domain (`echodesk.ge`)**: Public schema for tenant management and main dashboard
- **Tenant Subdomains (`*.echodesk.ge`)**: Each tenant gets its own subdomain with isolated database schema
- **Automatic Routing**: Subdomain-based tenant detection with fallback to public schema

## ✨ Features

- **Multi-tenancy**: Each tenant has its own subdomain and isolated database schema
- **User Management**: Tenant-specific user authentication and authorization
- **CRM Functionality**: Call logs, client management
- **REST API**: Fully documented API with Swagger/OpenAPI
- **Admin Interface**: Django admin for tenant and user management
- **Wildcard Subdomain Support**: Automatic tenant routing via subdomains

## 🌐 Domain Structure

```
echodesk.ge                    → Public schema (tenant management)
├── admin/                     → Main admin dashboard
├── api/docs/                  → API documentation
└── api/tenants/              → Tenant management API

demo.echodesk.ge              → Demo tenant schema
├── admin/                    → Tenant admin
├── api/users/                → Tenant users API
└── api/call-logs/            → Tenant CRM API

acme.echodesk.ge              → Acme tenant schema
├── admin/                    → Tenant admin
├── api/users/                → Tenant users API
└── api/call-logs/            → Tenant CRM API
```

## 🚀 Tech Stack

- Django 4.2+ with Django REST Framework
- PostgreSQL (required for schema-based multi-tenancy)
- django-tenant-schemas for multi-tenancy
- drf-spectacular for API documentation
- Custom middleware for domain routing

## 📋 Setup Instructions

### Prerequisites

- Python 3.8+
- PostgreSQL 12+
- Virtual environment (recommended)

### 1. Clone and Setup Environment

```bash
git clone https://github.com/giorgigordiashvili/echodesk-back.git
cd echodesk-back
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Environment Configuration

Copy the example environment file and configure it:

```bash
cp .env.example .env
```

Edit `.env` with your settings:

```bash
SECRET_KEY=your-very-secret-key-here
DEBUG=True
MAIN_DOMAIN=echodesk.ge
DB_NAME=echodesk_db
DB_USER=echodesk_user
DB_PASSWORD=your_password
DB_HOST=localhost
DB_PORT=5432
```

### 3. Build and Setup

Run the automated build script:

```bash
chmod +x build.sh
./build.sh
```

Or manually:

```bash
# Run shared schema migrations
python manage.py migrate_schemas --shared

# Create superuser for public schema
python manage.py createsuperuser

# Create your first tenant
python manage.py create_tenant demo "Demo Company" demo.echodesk.ge admin@demo.com "Demo Admin" --admin-password "demo123"

# Run tenant migrations
python manage.py migrate_schemas

# Start development server
python manage.py runserver
```

## 🌐 Access URLs

### Production (DigitalOcean)

- **Main Dashboard**: https://echodesk.ge/admin/
- **API Docs**: https://echodesk.ge/api/docs/
- **Demo Tenant**: https://demo.echodesk.ge/admin/
- **Acme Tenant**: https://acme.echodesk.ge/admin/

### Local Development

- **Main Dashboard**: http://localhost:8000/admin/
- **API Docs**: http://localhost:8000/api/docs/
- **Demo Tenant**: http://demo.localhost:8000/admin/
- **Acme Tenant**: http://acme.localhost:8000/admin/

### For Local Development

Add entries to your `/etc/hosts` file:

```bash
127.0.0.1 localhost
127.0.0.1 demo.localhost
127.0.0.1 acme.localhost
```

## 🔧 Management Commands

### Create a New Tenant

```bash
python manage.py create_tenant <schema_name> <name> <domain> <admin_email> <admin_name> [--admin-password <password>]
```

Example:

```bash
python manage.py create_tenant startup "Startup Inc" startup.echodesk.ge admin@startup.com "Jane Smith" --admin-password "startup123"
```

### List All Tenants

```bash
python manage.py shell -c "
from tenants.models import Tenant
for tenant in Tenant.objects.all():
    print(f'{tenant.name}: {tenant.domain_url}')
"
```

## 📡 API Endpoints

### Public Schema (Main Domain)

- `GET /api/tenants/` - List all tenants
- `POST /api/tenants/` - Create new tenant
- `GET /api/tenants/{id}/users/` - List tenant users
- `POST /api/tenants/{id}/create_admin_user/` - Create admin user for tenant

### Tenant Schema (Per Subdomain)

- `GET /api/users/` - List users in current tenant
- `GET /api/call-logs/` - List call logs
- `POST /api/call-logs/` - Create call log
- `GET /api/clients/` - List clients
- `POST /api/clients/` - Create client

## 🚀 Deployment on DigitalOcean

The project is configured for automatic deployment on DigitalOcean App Platform.

### DNS Configuration

Set up your DNS with the following records:

```
A     echodesk.ge          → [Your DigitalOcean App IP]
CNAME *.echodesk.ge        → echodesk.ge
```

### Environment Variables

In DigitalOcean App Platform, set these environment variables:

```
SECRET_KEY=your-production-secret-key
DEBUG=False
MAIN_DOMAIN=echodesk.ge
DB_NAME=your_database_name
DB_USER=your_database_user
DB_PASSWORD=your_database_password
DB_HOST=your_database_host
DB_PORT=your_database_port
```

### Automatic Deployment

1. Push to the `main` branch
2. DigitalOcean App Platform will automatically build and deploy
3. The build script handles migrations and setup

## 🏗️ Project Structure

```
├── amanati_crm/              # Main Django project
│   ├── settings.py           # Multi-tenant Django settings
│   ├── urls.py              # Tenant-specific URLs
│   ├── urls_public.py       # Public schema URLs
│   └── middleware.py        # Custom tenant routing middleware
├── tenants/                 # Tenant management app
│   ├── models.py            # Tenant model
│   ├── views.py             # Tenant management API
│   └── management/          # Management commands
├── users/                   # User management app
│   ├── models.py            # Custom User model
│   └── views.py             # User management API
├── crm/                     # CRM functionality
│   ├── models.py            # CallLog, Client models
│   ├── serializers.py       # API serializers
│   └── views.py             # CRM API endpoints
├── build.sh                 # Local build script
├── build_production.sh      # Production build script
└── .do/app.yaml            # DigitalOcean configuration
```

## 🔧 Development

### Adding New Tenant-Specific Features

1. Add models to apps in `TENANT_APPS`
2. Create migrations: `python manage.py makemigrations`
3. Apply to all tenants: `python manage.py migrate_schemas`

### Adding Public Features

1. Add models to apps in `SHARED_APPS`
2. Create migrations: `python manage.py makemigrations`
3. Apply to public schema: `python manage.py migrate_schemas --shared`

## 🐛 Troubleshooting

### Common Issues

1. **"Tenant not found"**: Check subdomain spelling and DNS configuration
2. **"relation does not exist"**: Run migrations for the correct schema
3. **CORS errors**: Verify domain configuration in settings

### Useful Commands

```bash
# Check system status
python manage.py check

# Test tenant setup
python test_tenants.py

# Access Django shell in tenant context
python manage.py tenant_command shell --schema=demo
```

## 📚 API Documentation

Visit `/api/docs/` on any domain for interactive Swagger documentation.

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## 📄 License

[Add your license information here]
