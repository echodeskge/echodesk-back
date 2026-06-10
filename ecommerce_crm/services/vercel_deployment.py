"""
Vercel Deployment Service for E-commerce Frontend

This service handles automated deployment of tenant-specific
e-commerce frontends to Vercel.

Multi-Tenant Architecture:
- A single shared Vercel project serves all tenants
- Each tenant is identified by their subdomain: {schema}.ecommerce.echodesk.ge
- Custom domains are also supported via the TenantDomain model
- Tenant configuration is resolved at runtime via middleware (not build-time env vars)
"""
import requests
import logging
import time
import secrets
import base64
from django.conf import settings
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

# Shared multi-tenant Vercel project configuration
# This project serves all tenants via wildcard domain *.ecommerce.echodesk.ge
SHARED_ECOMMERCE_PROJECT_ID = getattr(settings, 'VERCEL_SHARED_ECOMMERCE_PROJECT_ID', '')
SHARED_ECOMMERCE_PROJECT_NAME = getattr(settings, 'VERCEL_SHARED_ECOMMERCE_PROJECT_NAME', 'echodesk-ecommerce')
ECOMMERCE_DOMAIN_SUFFIX = '.ecommerce.echodesk.ge'


class VercelDeploymentService:
    """Service for managing Vercel deployments via REST API"""

    BASE_URL = "https://api.vercel.com"

    def __init__(self):
        self.token = getattr(settings, 'VERCEL_TOKEN', '')
        self.team_id = getattr(settings, 'VERCEL_TEAM_ID', '')
        self.github_repo = getattr(settings, 'VERCEL_GITHUB_REPO', '')

        if not self.token:
            raise ValueError("VERCEL_TOKEN not configured in settings")

        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

    def _get_team_param(self) -> str:
        """Get team ID query parameter if configured"""
        return f"?teamId={self.team_id}" if self.team_id else ""

    def get_project_domains(self, project_id: str) -> Dict[str, Any]:
        """
        Get all domains/aliases for a project including the production URL

        Args:
            project_id: Vercel project ID or name

        Returns:
            Dict with production_url and all domains
        """
        url = f"{self.BASE_URL}/v9/projects/{project_id}{self._get_team_param()}"

        response = requests.get(url, headers=self.headers)

        if response.status_code == 200:
            data = response.json()

            # Extract domains from project data
            targets = data.get("targets", {})
            production_target = targets.get("production", {})

            # Get the production alias (the .vercel.app URL)
            alias = production_target.get("alias", [])

            # Look for the main .vercel.app domain
            production_url = None
            for domain in alias:
                if ".vercel.app" in domain:
                    production_url = f"https://{domain}"
                    break

            # If no .vercel.app found, construct it from project name
            if not production_url:
                project_name = data.get("name", "")
                if project_name:
                    production_url = f"https://{project_name}.vercel.app"

            return {
                "success": True,
                "production_url": production_url,
                "all_domains": alias,
                "project_name": data.get("name"),
                "latest_deployments": data.get("latestDeployments", [])
            }
        else:
            return {
                "success": False,
                "error": "Failed to fetch project domains"
            }

    def wait_for_deployment(self, project_id: str, max_wait: int = 300) -> Dict[str, Any]:
        """
        Wait for the latest deployment to be ready

        Args:
            project_id: Project ID or name
            max_wait: Maximum seconds to wait (default 5 minutes)

        Returns:
            Deployment status and URL when ready
        """
        start_time = time.time()
        poll_interval = 10  # Poll every 10 seconds

        while time.time() - start_time < max_wait:
            project_info = self.get_project_domains(project_id)

            if not project_info.get("success"):
                time.sleep(poll_interval)
                continue

            latest_deployments = project_info.get("latest_deployments", [])

            if latest_deployments:
                latest = latest_deployments[0]
                state = latest.get("readyState", "")

                if state == "READY":
                    # Deployment is complete
                    return {
                        "success": True,
                        "state": state,
                        "production_url": project_info.get("production_url"),
                        "deployment_url": latest.get("url"),
                        "created_at": latest.get("createdAt")
                    }
                elif state in ["ERROR", "CANCELED"]:
                    return {
                        "success": False,
                        "state": state,
                        "error": f"Deployment {state.lower()}"
                    }
                else:
                    # Still building/queued
                    logger.info(f"Deployment state: {state}, waiting...")
                    time.sleep(poll_interval)
            else:
                time.sleep(poll_interval)

        return {
            "success": False,
            "error": f"Deployment did not complete within {max_wait} seconds"
        }

    def create_project(self, project_name: str, env_vars: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Create a new Vercel project for a tenant

        Args:
            project_name: Unique project name (e.g., store-demo)
            env_vars: List of environment variable dicts

        Returns:
            Project creation response including project ID and URLs
        """
        url = f"{self.BASE_URL}/v9/projects{self._get_team_param()}"

        # Prepare environment variables in Vercel format
        formatted_env_vars = []
        for env in env_vars:
            formatted_env_vars.append({
                "key": env["key"],
                "value": str(env["value"]),
                "target": ["production", "preview", "development"],
                "type": "plain"
            })

        payload = {
            "name": project_name,
            "framework": "nextjs",
            "gitRepository": {
                "type": "github",
                "repo": self.github_repo
            },
            "environmentVariables": formatted_env_vars
        }

        logger.info(f"Creating Vercel project: {project_name}")

        response = requests.post(url, headers=self.headers, json=payload)

        if response.status_code == 200:
            project_data = response.json()
            project_id = project_data.get("id")
            logger.info(f"Project created successfully: {project_id}")

            # Update project settings to force build regardless of SHA
            self._configure_build_settings(project_id)

            return {
                "success": True,
                "project_id": project_id,
                "project_name": project_data.get("name"),
                "account_id": project_data.get("accountId"),
                "created_at": project_data.get("createdAt"),
                "framework": project_data.get("framework"),
                "url": f"https://{project_name}.vercel.app"
            }
        else:
            error_data = response.json()
            logger.error(f"Failed to create project: {error_data}")
            return {
                "success": False,
                "error": error_data.get("error", {}).get("message", "Unknown error"),
                "code": error_data.get("error", {}).get("code", "UNKNOWN")
            }

    def _configure_build_settings(self, project_id: str) -> Dict[str, Any]:
        """
        Configure project build settings to force deployment

        Args:
            project_id: Vercel project ID

        Returns:
            Operation result
        """
        url = f"{self.BASE_URL}/v9/projects/{project_id}{self._get_team_param()}"

        # Set ignored build step to always build (exit 1 = build needed)
        payload = {
            "commandForIgnoringBuildStep": "exit 1",
            "autoExposeSystemEnvs": True
        }

        logger.info(f"Configuring build settings for project {project_id}")
        response = requests.patch(url, headers=self.headers, json=payload)

        if response.status_code == 200:
            logger.info(f"Build settings configured successfully")
            return {"success": True}
        else:
            error_data = response.json() if response.text else {}
            logger.warning(f"Failed to configure build settings: {error_data}")
            return {
                "success": False,
                "error": error_data.get("error", {}).get("message", "Unknown error")
            }

    def add_environment_variables(self, project_id: str, env_vars: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Add or update environment variables for a project

        Args:
            project_id: Vercel project ID
            env_vars: List of {key, value} dicts

        Returns:
            Operation result
        """
        url = f"{self.BASE_URL}/v9/projects/{project_id}/env{self._get_team_param()}"
        if "?" in url:
            url += "&upsert=true"
        else:
            url += "?upsert=true"

        # Format for batch update
        formatted_vars = []
        for env in env_vars:
            formatted_vars.append({
                "key": env["key"],
                "value": str(env["value"]),
                "target": ["production", "preview", "development"],
                "type": "plain"
            })

        response = requests.post(url, headers=self.headers, json=formatted_vars)

        if response.status_code in [200, 201]:
            logger.info(f"Environment variables updated for project {project_id}")
            return {"success": True, "updated": len(formatted_vars)}
        else:
            error_data = response.json()
            logger.error(f"Failed to update env vars: {error_data}")
            return {
                "success": False,
                "error": error_data.get("error", {}).get("message", "Unknown error")
            }

    def trigger_deployment(self, project_name: str) -> Dict[str, Any]:
        """
        Trigger a new deployment for a project using Deploy Hooks

        Args:
            project_name: Project name or ID

        Returns:
            Deployment information
        """
        logger.info(f"Triggering deployment for project: {project_name}")

        # First get the project to get its ID
        project_info = self.get_project(project_name)
        if not project_info.get("success"):
            return {
                "success": False,
                "error": f"Project not found: {project_name}"
            }

        project_id = project_info.get("id")

        # METHOD 1 (PREFERRED): Use Deploy Hooks - bypasses SHA check
        logger.info(f"Creating deploy hook for project {project_name}")
        hook_url = f"{self.BASE_URL}/v9/projects/{project_name}/deploy-hooks{self._get_team_param()}"

        hook_payload = {
            "name": f"api-trigger-{int(time.time())}",  # Unique name
            "ref": "main"
        }

        hook_response = requests.post(hook_url, headers=self.headers, json=hook_payload)
        logger.info(f"Deploy hook creation status: {hook_response.status_code}")

        if hook_response.status_code in [200, 201]:
            hook_data = hook_response.json()
            logger.info(f"Deploy hook response received")

            # The URL is in the link.deployHooks array of the project response
            # We need to find our newly created hook
            link_data = hook_data.get("link", {})
            deploy_hooks = link_data.get("deployHooks", [])

            deploy_hook_url = None
            for hook in deploy_hooks:
                if hook.get("url"):
                    deploy_hook_url = hook.get("url")
                    logger.info(f"Found deploy hook URL in response: {deploy_hook_url}")
                    break

            # If still not found, look for direct url field
            if not deploy_hook_url:
                deploy_hook_url = hook_data.get("url") or hook_data.get("deploymentUrl")
                if deploy_hook_url:
                    logger.info(f"Found deploy hook URL directly: {deploy_hook_url}")

            if deploy_hook_url:
                logger.info(f"Calling deploy hook URL: {deploy_hook_url}")
                trigger_response = requests.post(deploy_hook_url)
                logger.info(f"Deploy hook response: {trigger_response.status_code}")

                if trigger_response.status_code in [200, 201]:
                    trigger_data = trigger_response.json()
                    job_info = trigger_data.get("job", {})
                    logger.info(f"Deployment triggered via hook! Job ID: {job_info.get('id')}")
                    return {
                        "success": True,
                        "deployment_id": job_info.get("id"),
                        "url": None,
                        "ready_state": "QUEUED",
                        "created_at": job_info.get("createdAt")
                    }
                else:
                    logger.warning(f"Hook trigger failed: {trigger_response.status_code} - {trigger_response.text[:500]}")
            else:
                logger.warning(f"No deploy hook URL in response: {hook_data}")
        else:
            hook_error = hook_response.json() if hook_response.text else {}
            logger.warning(f"Failed to create deploy hook: {hook_error}")

        # METHOD 2 (FALLBACK): Try direct deployment API with forceNew
        logger.info(f"Falling back to direct deployment API...")
        deploy_url = f"{self.BASE_URL}/v13/deployments{self._get_team_param()}"
        if "?" in deploy_url:
            deploy_url += "&forceNew=1"
        else:
            deploy_url += "?forceNew=1"

        deploy_payload = {
            "name": project_name,
            "target": "production",
            "gitSource": {
                "type": "github",
                "repo": self.github_repo,
                "ref": "main"
            }
        }

        logger.info(f"Creating deployment with payload: {deploy_payload}")
        deploy_response = requests.post(deploy_url, headers=self.headers, json=deploy_payload)

        if deploy_response.status_code in [200, 201]:
            deployment_data = deploy_response.json()
            logger.info(f"Deployment created via direct API: {deployment_data.get('id')}")
            return {
                "success": True,
                "deployment_id": deployment_data.get("id"),
                "url": deployment_data.get("url"),
                "ready_state": deployment_data.get("readyState"),
                "created_at": deployment_data.get("createdAt")
            }

        error_data = deploy_response.json() if deploy_response.text else {}
        logger.error(f"Direct deployment also failed: {error_data}")

        return {
            "success": False,
            "error": error_data.get("error", {}).get("message", "Failed to trigger deployment via both methods")
        }

    def get_deployment_status(self, deployment_id: str) -> Dict[str, Any]:
        """
        Get the status of a deployment

        Args:
            deployment_id: Vercel deployment ID

        Returns:
            Deployment status information
        """
        url = f"{self.BASE_URL}/v13/deployments/{deployment_id}{self._get_team_param()}"

        response = requests.get(url, headers=self.headers)

        if response.status_code == 200:
            data = response.json()
            return {
                "success": True,
                "id": data.get("id"),
                "state": data.get("readyState"),
                "url": data.get("url"),
                "created_at": data.get("createdAt"),
                "ready_at": data.get("ready"),
                "alias": data.get("alias", [])
            }
        else:
            return {"success": False, "error": "Failed to get deployment status"}

    def get_project(self, project_name: str) -> Dict[str, Any]:
        """
        Get project information

        Args:
            project_name: Project name or ID

        Returns:
            Project information
        """
        url = f"{self.BASE_URL}/v9/projects/{project_name}{self._get_team_param()}"

        response = requests.get(url, headers=self.headers)

        if response.status_code == 200:
            data = response.json()
            return {
                "success": True,
                "id": data.get("id"),
                "name": data.get("name"),
                "framework": data.get("framework"),
                "latest_deployments": data.get("latestDeployments", [])
            }
        else:
            return {"success": False, "error": "Project not found"}

    def delete_project(self, project_id: str) -> Dict[str, Any]:
        """
        Delete a Vercel project

        Args:
            project_id: Project ID to delete

        Returns:
            Operation result
        """
        url = f"{self.BASE_URL}/v9/projects/{project_id}{self._get_team_param()}"

        response = requests.delete(url, headers=self.headers)

        if response.status_code in [200, 204]:
            logger.info(f"Project {project_id} deleted successfully")
            return {"success": True}
        else:
            error_data = response.json() if response.text else {}
            logger.error(f"Failed to delete project: {error_data}")
            return {
                "success": False,
                "error": error_data.get("error", {}).get("message", "Unknown error")
            }

    def add_domain(self, project_id: str, domain: str) -> Dict[str, Any]:
        """
        Add a custom domain to a project

        Args:
            project_id: Vercel project ID
            domain: Domain name to add (e.g., store.example.com)

        Returns:
            Domain configuration including DNS records needed
        """
        url = f"{self.BASE_URL}/v9/projects/{project_id}/domains{self._get_team_param()}"

        payload = {
            "name": domain
        }

        logger.info(f"Adding domain {domain} to project {project_id}")
        response = requests.post(url, headers=self.headers, json=payload)

        if response.status_code in [200, 201]:
            domain_data = response.json()
            logger.info(f"Domain added successfully: {domain}")

            # Get verification records
            verification = domain_data.get("verification", [])

            return {
                "success": True,
                "domain": domain_data.get("name"),
                "verified": domain_data.get("verified", False),
                "verification": verification,
                "apexName": domain_data.get("apexName"),
                "createdAt": domain_data.get("createdAt"),
                "gitBranch": domain_data.get("gitBranch"),
                "redirect": domain_data.get("redirect"),
                "redirectStatusCode": domain_data.get("redirectStatusCode")
            }
        else:
            error_data = response.json() if response.text else {}
            logger.error(f"Failed to add domain: {error_data}")
            return {
                "success": False,
                "error": error_data.get("error", {}).get("message", "Unknown error")
            }

    def remove_domain(self, project_id: str, domain: str) -> Dict[str, Any]:
        """
        Remove a custom domain from a project

        Args:
            project_id: Vercel project ID
            domain: Domain name to remove

        Returns:
            Operation result
        """
        url = f"{self.BASE_URL}/v9/projects/{project_id}/domains/{domain}{self._get_team_param()}"

        logger.info(f"Removing domain {domain} from project {project_id}")
        response = requests.delete(url, headers=self.headers)

        if response.status_code in [200, 204]:
            logger.info(f"Domain {domain} removed successfully")
            return {"success": True}
        else:
            error_data = response.json() if response.text else {}
            logger.error(f"Failed to remove domain: {error_data}")
            return {
                "success": False,
                "error": error_data.get("error", {}).get("message", "Unknown error")
            }

    def get_domains(self, project_id: str) -> Dict[str, Any]:
        """
        Get all domains for a project

        Args:
            project_id: Vercel project ID

        Returns:
            List of domains with their configuration
        """
        url = f"{self.BASE_URL}/v9/projects/{project_id}/domains{self._get_team_param()}"

        response = requests.get(url, headers=self.headers)

        if response.status_code == 200:
            data = response.json()
            domains = data.get("domains", [])

            formatted_domains = []
            for domain in domains:
                formatted_domains.append({
                    "name": domain.get("name"),
                    "verified": domain.get("verified", False),
                    "verification": domain.get("verification", []),
                    "apexName": domain.get("apexName"),
                    "createdAt": domain.get("createdAt"),
                    "gitBranch": domain.get("gitBranch"),
                    "redirect": domain.get("redirect"),
                    "redirectStatusCode": domain.get("redirectStatusCode")
                })

            return {
                "success": True,
                "domains": formatted_domains
            }
        else:
            error_data = response.json() if response.text else {}
            logger.error(f"Failed to get domains: {error_data}")
            return {
                "success": False,
                "error": error_data.get("error", {}).get("message", "Unknown error")
            }

    def verify_domain(self, project_id: str, domain: str) -> Dict[str, Any]:
        """
        Verify a domain's DNS configuration

        Args:
            project_id: Vercel project ID
            domain: Domain to verify

        Returns:
            Verification status
        """
        url = f"{self.BASE_URL}/v9/projects/{project_id}/domains/{domain}/verify{self._get_team_param()}"

        response = requests.post(url, headers=self.headers)

        if response.status_code == 200:
            data = response.json()
            return {
                "success": True,
                "verified": data.get("verified", False),
                "verification": data.get("verification", [])
            }
        else:
            error_data = response.json() if response.text else {}
            return {
                "success": False,
                "error": error_data.get("error", {}).get("message", "Unknown error"),
                "verified": False
            }


def deploy_tenant_frontend(tenant) -> Dict[str, Any]:
    """
    High-level function to deploy a tenant's e-commerce frontend

    Multi-Tenant Approach:
    - Adds tenant's subdomain to a single shared Vercel project
    - No new project is created; all tenants share one deployment
    - Tenant config is resolved at runtime via middleware (not build-time env vars)

    Args:
        tenant: Tenant model instance

    Returns:
        Deployment result with URL and status
    """
    service = VercelDeploymentService()

    # Use shared multi-tenant project
    project_id = SHARED_ECOMMERCE_PROJECT_ID
    project_name = SHARED_ECOMMERCE_PROJECT_NAME

    if not project_id:
        logger.error("VERCEL_SHARED_ECOMMERCE_PROJECT_ID not configured")
        return {
            "success": False,
            "error": "Multi-tenant Vercel project not configured. Please set VERCEL_SHARED_ECOMMERCE_PROJECT_ID in settings."
        }

    # Generate subdomain for this tenant
    subdomain = f"{tenant.schema_name}{ECOMMERCE_DOMAIN_SUFFIX}"
    frontend_url = f"https://{subdomain}"

    logger.info(f"Adding tenant {tenant.schema_name} to shared ecommerce project")
    logger.info(f"Subdomain: {subdomain}")

    # Add subdomain to the shared project
    domain_result = service.add_domain(project_id, subdomain)

    if domain_result.get("success"):
        logger.info(f"Subdomain {subdomain} added to shared project successfully")

        # Include DNS configuration for unverified domains
        dns_config = None
        if not domain_result.get("verified", False):
            verification = domain_result.get("verification", [])
            dns_config = {
                "instructions": "Configure the following DNS record at your domain provider:",
                "records": verification,
                "note": "DNS propagation may take up to 48 hours, but usually completes within minutes."
            }

        return {
            "success": True,
            "project_id": project_id,
            "project_name": project_name,
            "url": frontend_url,
            "domain": subdomain,
            "verified": domain_result.get("verified", False),
            "dns_config": dns_config,
            "message": f"Tenant storefront is now available at {frontend_url}" if domain_result.get("verified", False) else f"Domain {subdomain} added. Configure DNS to activate."
        }
    else:
        # Check if domain already exists (which is fine)
        error_msg = domain_result.get("error", "")
        if "already exists" in error_msg.lower() or "domain is already" in error_msg.lower():
            logger.info(f"Subdomain {subdomain} already exists on shared project - this is fine")
            return {
                "success": True,
                "project_id": project_id,
                "project_name": project_name,
                "url": frontend_url,
                "domain": subdomain,
                "verified": True,
                "message": f"Tenant storefront is available at {frontend_url}"
            }

        logger.error(f"Failed to add subdomain for {tenant.schema_name}: {domain_result.get('error')}")
        return {
            "success": False,
            "error": domain_result.get("error", "Failed to add subdomain to shared project")
        }


def deploy_tenant_frontend_legacy(tenant) -> Dict[str, Any]:
    """
    LEGACY: Creates a separate Vercel project per tenant.

    This is the old approach - kept for reference/rollback purposes.
    The new approach uses a single shared multi-tenant project.

    Args:
        tenant: Tenant model instance

    Returns:
        Deployment result with URL and status
    """
    service = VercelDeploymentService()

    # Generate project name (must be URL-safe)
    project_name = f"store-{tenant.schema_name}".lower().replace("_", "-")

    # Generate NextAuth secret (unique per tenant)
    nextauth_secret = base64.b64encode(secrets.token_bytes(32)).decode('utf-8')
    frontend_url = f"https://{project_name}.vercel.app"

    # Prepare environment variables from tenant configuration
    env_vars = [
        {"key": "NEXT_PUBLIC_TENANT_ID", "value": str(tenant.id)},
        {"key": "NEXT_PUBLIC_TENANT_SCHEMA", "value": tenant.schema_name},
        {"key": "NEXT_PUBLIC_API_URL", "value": f"https://{tenant.schema_name}.api.echodesk.ge"},
        {"key": "NEXT_PUBLIC_STORE_NAME", "value": tenant.name},
        {"key": "NEXT_PUBLIC_STORE_DESCRIPTION", "value": getattr(tenant, 'description', 'Welcome to our store')},
        {"key": "NEXT_PUBLIC_STORE_LOGO", "value": getattr(tenant, 'logo_url', '')},
        {"key": "NEXT_PUBLIC_PRIMARY_COLOR", "value": ""},  # Use default colors
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

    # Create project with environment variables
    result = service.create_project(project_name, env_vars)

    if result.get("success"):
        project_id = result.get("project_id")
        logger.info(f"Project created: {project_id}")

        # Set the expected URL immediately
        result["url"] = f"https://{project_name}.vercel.app"

        # Give Vercel a moment to finish project setup
        time.sleep(2)

        # ALWAYS trigger a manual deployment after project creation
        # This is necessary because:
        # 1. Project settings (commandForIgnoringBuildStep) are now configured
        # 2. The initial auto-deployment may have been skipped due to SHA check
        # 3. Manual trigger uses forceNew=1 which bypasses SHA check
        logger.info(f"Triggering deployment for new project...")
        deployment_result = service.trigger_deployment(project_name)

        if deployment_result.get("success"):
            logger.info(f"Deployment triggered successfully: {deployment_result.get('deployment_id')}")
            result["deployment_triggered"] = True
            result["deployment_id"] = deployment_result.get("deployment_id")
        else:
            logger.warning(f"Failed to trigger deployment: {deployment_result.get('error')}")
            result["deployment_error"] = deployment_result.get('error')

            # Check if there's an existing deployment (unlikely but possible)
            project_info = service.get_project_domains(project_id)
            if project_info.get("success"):
                latest_deployments = project_info.get("latest_deployments", [])
                if latest_deployments:
                    latest = latest_deployments[0]
                    state = latest.get("readyState", "")
                    logger.info(f"Found existing deployment with state: {state}")
                    if state == "READY":
                        actual_url = project_info.get("production_url")
                        if actual_url:
                            result["url"] = actual_url

        logger.info(f"Tenant {tenant.schema_name} frontend project created. Expected URL: {result['url']}")
        logger.info(f"Note: Deployment builds in background on Vercel (1-2 min to be live)")
    else:
        logger.error(f"Failed to deploy frontend for {tenant.schema_name}: {result.get('error')}")

    return result
