"""
Netlify Deployment Service for E-commerce Frontend

This service handles automated deployment of tenant-specific
e-commerce frontends to Netlify.
"""
import requests
import logging
import time
import secrets
import base64
from django.conf import settings
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


class NetlifyDeploymentService:
    """Service for managing Netlify deployments via REST API"""

    BASE_URL = "https://api.netlify.com/api/v1"

    def __init__(self):
        self.token = getattr(settings, 'NETLIFY_TOKEN', '')
        self.team_slug = getattr(settings, 'NETLIFY_TEAM_SLUG', '')
        self.github_repo = getattr(settings, 'NETLIFY_GITHUB_REPO', '') or getattr(settings, 'VERCEL_GITHUB_REPO', '')

        if not self.token:
            raise ValueError("NETLIFY_TOKEN not configured in settings")

        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

    def create_site(self, site_name: str, env_vars: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Create a new Netlify site for a tenant

        Args:
            site_name: Unique site name (e.g., store-demo)
            env_vars: List of environment variable dicts

        Returns:
            Site creation response including site ID and URLs
        """
        url = f"{self.BASE_URL}/sites"

        # Prepare environment variables
        env_dict = {env["key"]: str(env["value"]) for env in env_vars}

        payload = {
            "name": site_name,
            "custom_domain": None,
            "force_ssl": True,
            "processing_settings": {
                "html": {"pretty_urls": True}
            }
        }

        # Add team if configured
        if self.team_slug:
            payload["account_slug"] = self.team_slug

        logger.info(f"Creating Netlify site: {site_name}")

        response = requests.post(url, headers=self.headers, json=payload)

        if response.status_code in [200, 201]:
            site_data = response.json()
            site_id = site_data.get("id")
            logger.info(f"Site created successfully: {site_id}")

            # Set environment variables
            if env_vars:
                env_result = self.set_environment_variables(site_id, env_vars)
                if not env_result.get("success"):
                    logger.warning(f"Failed to set env vars: {env_result.get('error')}")

            # Link to GitHub repo
            repo_result = self.link_repository(site_id)
            if repo_result.get("success"):
                logger.info(f"Repository linked successfully")
            else:
                logger.warning(f"Failed to link repository: {repo_result.get('error')}")

            return {
                "success": True,
                "site_id": site_id,
                "site_name": site_data.get("name"),
                "url": site_data.get("ssl_url") or site_data.get("url") or f"https://{site_name}.netlify.app",
                "admin_url": site_data.get("admin_url"),
                "created_at": site_data.get("created_at")
            }
        else:
            error_data = response.json() if response.text else {}
            logger.error(f"Failed to create site: {error_data}")
            return {
                "success": False,
                "error": error_data.get("message", str(error_data)),
                "code": response.status_code
            }

    def set_environment_variables(self, site_id: str, env_vars: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Set environment variables for a site

        Args:
            site_id: Netlify site ID
            env_vars: List of {key, value} dicts

        Returns:
            Operation result
        """
        # Netlify uses a different endpoint for env vars
        url = f"{self.BASE_URL}/accounts/{self.team_slug}/env" if self.team_slug else f"{self.BASE_URL}/sites/{site_id}/env"

        success_count = 0
        errors = []

        for env in env_vars:
            env_url = f"{self.BASE_URL}/sites/{site_id}/env/{env['key']}"

            # Try to update first, then create if not exists
            payload = {
                "key": env["key"],
                "values": [
                    {
                        "value": str(env["value"]),
                        "context": "all"
                    }
                ]
            }

            # First try PATCH to update
            response = requests.patch(env_url, headers=self.headers, json=payload)

            if response.status_code not in [200, 201]:
                # Try POST to create
                create_url = f"{self.BASE_URL}/sites/{site_id}/env"
                response = requests.post(create_url, headers=self.headers, json=[payload])

            if response.status_code in [200, 201]:
                success_count += 1
            else:
                errors.append(f"{env['key']}: {response.status_code}")

        if errors:
            logger.warning(f"Some env vars failed: {errors}")
            return {
                "success": len(errors) == 0,
                "updated": success_count,
                "errors": errors
            }

        logger.info(f"Environment variables set for site {site_id}")
        return {"success": True, "updated": success_count}

    def link_repository(self, site_id: str) -> Dict[str, Any]:
        """
        Link a GitHub repository to the site

        Args:
            site_id: Netlify site ID

        Returns:
            Operation result
        """
        if not self.github_repo:
            return {"success": False, "error": "NETLIFY_GITHUB_REPO not configured"}

        url = f"{self.BASE_URL}/sites/{site_id}"

        # Parse repo format: "owner/repo"
        repo_parts = self.github_repo.split("/")
        if len(repo_parts) != 2:
            return {"success": False, "error": f"Invalid repo format: {self.github_repo}"}

        payload = {
            "repo": {
                "provider": "github",
                "repo": self.github_repo,
                "branch": "main",
                "cmd": "npm run build",
                "dir": ".next",  # Next.js output directory
                "installation_id": None,  # Will use OAuth
                "env": {},
                "functions_dir": None,
                "private_logs": False,
                "public_repo": False
            }
        }

        response = requests.patch(url, headers=self.headers, json=payload)

        if response.status_code in [200, 201]:
            return {"success": True}
        else:
            error_data = response.json() if response.text else {}
            return {
                "success": False,
                "error": error_data.get("message", str(error_data))
            }

    def trigger_build(self, site_id: str) -> Dict[str, Any]:
        """
        Trigger a new build for a site

        Args:
            site_id: Netlify site ID

        Returns:
            Build information
        """
        url = f"{self.BASE_URL}/sites/{site_id}/builds"

        logger.info(f"Triggering build for site: {site_id}")
        response = requests.post(url, headers=self.headers, json={})

        if response.status_code in [200, 201]:
            build_data = response.json()
            logger.info(f"Build triggered: {build_data.get('id')}")
            return {
                "success": True,
                "build_id": build_data.get("id"),
                "state": build_data.get("state"),
                "created_at": build_data.get("created_at")
            }
        else:
            error_data = response.json() if response.text else {}
            logger.error(f"Failed to trigger build: {error_data}")
            return {
                "success": False,
                "error": error_data.get("message", str(error_data))
            }

    def get_site(self, site_id: str) -> Dict[str, Any]:
        """
        Get site information

        Args:
            site_id: Site ID or name

        Returns:
            Site information
        """
        url = f"{self.BASE_URL}/sites/{site_id}"

        response = requests.get(url, headers=self.headers)

        if response.status_code == 200:
            data = response.json()
            return {
                "success": True,
                "id": data.get("id"),
                "name": data.get("name"),
                "url": data.get("ssl_url") or data.get("url"),
                "state": data.get("state"),
                "published_deploy": data.get("published_deploy", {})
            }
        else:
            return {"success": False, "error": "Site not found"}

    def delete_site(self, site_id: str) -> Dict[str, Any]:
        """
        Delete a Netlify site

        Args:
            site_id: Site ID to delete

        Returns:
            Operation result
        """
        url = f"{self.BASE_URL}/sites/{site_id}"

        response = requests.delete(url, headers=self.headers)

        if response.status_code in [200, 204]:
            logger.info(f"Site {site_id} deleted successfully")
            return {"success": True}
        else:
            error_data = response.json() if response.text else {}
            logger.error(f"Failed to delete site: {error_data}")
            return {
                "success": False,
                "error": error_data.get("message", str(error_data))
            }

    def wait_for_deploy(self, site_id: str, max_wait: int = 60) -> Dict[str, Any]:
        """
        Wait for the latest deploy to be ready (brief check)

        Args:
            site_id: Site ID
            max_wait: Maximum seconds to wait

        Returns:
            Deploy status
        """
        start_time = time.time()

        while time.time() - start_time < max_wait:
            site_info = self.get_site(site_id)

            if site_info.get("success"):
                published_deploy = site_info.get("published_deploy", {})
                state = published_deploy.get("state", "")

                if state == "ready":
                    return {
                        "success": True,
                        "state": state,
                        "url": site_info.get("url")
                    }
                elif state in ["error", "failed"]:
                    return {
                        "success": False,
                        "state": state,
                        "error": "Deploy failed"
                    }

            time.sleep(5)

        return {"success": False, "error": "Deploy check timeout"}


def deploy_tenant_frontend_netlify(tenant) -> Dict[str, Any]:
    """
    High-level function to deploy a tenant's e-commerce frontend to Netlify

    Args:
        tenant: Tenant model instance

    Returns:
        Deployment result with URL and status
    """
    service = NetlifyDeploymentService()

    # Generate site name (must be URL-safe)
    site_name = f"store-{tenant.schema_name}".lower().replace("_", "-")

    # Generate NextAuth secret (unique per tenant)
    nextauth_secret = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')
    frontend_url = f"https://{site_name}.netlify.app"

    # Prepare environment variables from tenant configuration
    env_vars = [
        {"key": "NEXT_PUBLIC_TENANT_ID", "value": str(tenant.id)},
        {"key": "NEXT_PUBLIC_TENANT_SCHEMA", "value": tenant.schema_name},
        {"key": "NEXT_PUBLIC_API_URL", "value": f"https://{tenant.schema_name}.api.echodesk.ge"},
        {"key": "NEXT_PUBLIC_STORE_NAME", "value": tenant.name},
        {"key": "NEXT_PUBLIC_STORE_DESCRIPTION", "value": getattr(tenant, 'description', 'Welcome to our store')},
        {"key": "NEXT_PUBLIC_STORE_LOGO", "value": getattr(tenant, 'logo_url', '')},
        {"key": "NEXT_PUBLIC_PRIMARY_COLOR", "value": ""},
        {"key": "NEXT_PUBLIC_SECONDARY_COLOR", "value": ""},
        {"key": "NEXT_PUBLIC_ACCENT_COLOR", "value": ""},
        {"key": "NEXT_PUBLIC_CURRENCY", "value": "GEL"},
        {"key": "NEXT_PUBLIC_LOCALE", "value": "en"},
        {"key": "NEXT_PUBLIC_ENABLE_WISHLIST", "value": "true"},
        {"key": "NEXT_PUBLIC_ENABLE_REVIEWS", "value": "false"},
        {"key": "NEXT_PUBLIC_ENABLE_COMPARE", "value": "false"},
        {"key": "NEXT_PUBLIC_FACEBOOK_URL", "value": ""},
        {"key": "NEXT_PUBLIC_INSTAGRAM_URL", "value": ""},
        {"key": "NEXT_PUBLIC_TWITTER_URL", "value": ""},
        {"key": "NEXT_PUBLIC_CONTACT_EMAIL", "value": getattr(tenant, 'email', f"support@{tenant.schema_name}.echodesk.ge")},
        {"key": "NEXT_PUBLIC_CONTACT_PHONE", "value": getattr(tenant, 'phone', '')},
        {"key": "NEXT_PUBLIC_CONTACT_ADDRESS", "value": getattr(tenant, 'address', '')},
        {"key": "NEXT_PUBLIC_GA_ID", "value": ""},
        {"key": "NEXT_PUBLIC_GTM_ID", "value": ""},
        {"key": "NEXT_PUBLIC_IMAGE_HOSTNAMES", "value": settings.AWS_S3_CUSTOM_DOMAIN},
        # NextAuth.js configuration (required for authentication)
        {"key": "NEXTAUTH_SECRET", "value": nextauth_secret},
        {"key": "NEXTAUTH_URL", "value": frontend_url},
    ]

    # Create site with environment variables
    result = service.create_site(site_name, env_vars)

    if result.get("success"):
        site_id = result.get("site_id")
        logger.info(f"Site created: {site_id}")

        # Set the expected URL
        result["url"] = f"https://{site_name}.netlify.app"

        # Trigger initial build
        build_result = service.trigger_build(site_id)
        if build_result.get("success"):
            logger.info(f"Initial build triggered: {build_result.get('build_id')}")
            result["build_triggered"] = True
        else:
            logger.warning(f"Failed to trigger build: {build_result.get('error')}")
            result["build_error"] = build_result.get('error')

        logger.info(f"Tenant {tenant.schema_name} frontend site created on Netlify. URL: {result['url']}")
        logger.info(f"Note: Build is happening in background (2-5 min to be live)")
    else:
        logger.error(f"Failed to create Netlify site for {tenant.schema_name}: {result.get('error')}")

    return result
