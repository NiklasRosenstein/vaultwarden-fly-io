// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
	site: 'https://niklasrosenstein.github.io',
	base: '/vaultwarden-fly-io',
	integrations: [
		starlight({
			title: 'Vaultwarden on Fly.io',
			description:
				'Run Vaultwarden on Fly.io for about 2 USD a month, with Litestream replication, S3-backed attachments and encrypted recovery backups.',
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
					label: 'Getting started',
					items: ['getting-started/introduction', 'getting-started/installation'],
				},
				{
					label: 'Guides',
					items: ['guides/migration', 'guides/backups', 'guides/recovery'],
				},
				{
					label: 'Reference',
					items: ['reference/configuration', 'reference/architecture'],
				},
				{ label: 'Development', link: '/development/' },
			],
		}),
	],
});
