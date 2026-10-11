// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// Pull request previews are served from a subdirectory, see .github/workflows/docs.yaml.
const base = (process.env.DOCS_BASE || '/vaultwarden-fly-io').replace(/\/$/, '');

// Old page paths, so existing links keep working.
const redirects = Object.fromEntries(
	Object.entries({
		'getting-started/introduction': 'get-started/what-this-is',
		'getting-started/installation': 'get-started/install',
		'guides/migration': 'get-started/move-from-another-server',
		'guides/backups': 'keep-your-data-safe/turn-on-backups',
		'guides/recovery': 'keep-your-data-safe/restore-from-a-backup',
		'guides/kubernetes': 'other-setups/docker-and-kubernetes',
		'guides/aws-oidc': 'other-setups/aws-s3-without-access-keys',
		'reference/configuration': 'reference/settings',
		'reference/architecture': 'reference/how-it-works',
	}).map(([from, to]) => [`/${from}`, `${base}/${to}/`]),
);

export default defineConfig({
	site: 'https://niklasrosenstein.github.io',
	base,
	// GitHub Pages serves this site from a branch, where Jekyll would drop directories starting with "_".
	build: { assets: 'assets' },
	redirects,
	integrations: [
		starlight({
			title: 'Vaultwarden on Fly.io',
			description:
				'Run your own Vaultwarden password manager on Fly.io for about 2 USD a month, with all data stored safely in S3.',
			logo: { src: './src/assets/logo.svg' },
			favicon: '/favicon.svg',
			social: [
				{
					icon: 'github',
					label: 'GitHub',
					href: 'https://github.com/NiklasRosenstein/vaultwarden-fly-io',
				},
			],
			editLink: {
				baseUrl: 'https://github.com/NiklasRosenstein/vaultwarden-fly-io/edit/main/docs/',
			},
			lastUpdated: true,
			customCss: [
				'@fontsource-variable/inter',
				'@fontsource-variable/jetbrains-mono',
				'./src/styles/custom.css',
			],
			sidebar: [
				{
					label: 'Get started',
					items: [
						'get-started/what-this-is',
						'get-started/install',
						'get-started/move-from-another-server',
					],
				},
				{
					label: 'Keep your data safe',
					items: ['keep-your-data-safe/turn-on-backups', 'keep-your-data-safe/restore-from-a-backup'],
				},
				{
					label: 'Other setups',
					items: ['other-setups/docker-and-kubernetes', 'other-setups/aws-s3-without-access-keys'],
				},
				{
					label: 'Reference',
					items: ['reference/settings', 'reference/how-it-works', 'reference/costs'],
				},
				{ label: 'Development', link: '/development/' },
			],
		}),
	],
});
