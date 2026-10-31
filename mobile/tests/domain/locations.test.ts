import { HIDDEN_LOCATION_IDS } from '@/constants/business';
import { isHiddenLocation, publicLocations } from '@/domain/locations';
import { createMockRepositories } from '@/services/mock';
import { LOCATIONS } from '@/services/mock/catalog';

describe('hidden cafés', () => {
  it('keeps Akuna St out of the public list but not the other cafés', () => {
    const ids = publicLocations(LOCATIONS).map((l) => l.id);
    expect(HIDDEN_LOCATION_IDS).toEqual(['akuna']);
    expect(ids).not.toContain('akuna');
    expect(ids).toHaveLength(LOCATIONS.length - 1);
    expect(ids).toEqual(LOCATIONS.filter((l) => l.id !== 'akuna').map((l) => l.id));
    expect(isHiddenLocation('akuna')).toBe(true);
    expect(isHiddenLocation('phillip')).toBe(false);
  });

  it('is applied by the mock repository, including direct lookups', async () => {
    const repos = createMockRepositories({ getSession: () => null, onSessionRefreshed() {}, onSessionExpired() {} });
    const listed = await repos.locations.getLocations();
    expect(listed.map((l) => l.id)).not.toContain('akuna');
    await expect(repos.locations.getLocation('akuna')).rejects.toMatchObject({ code: 'NOT_FOUND' });
    await expect(repos.locations.getLocation('phillip')).resolves.toMatchObject({ id: 'phillip' });
    // The underlying records are untouched, so history keeps its café.
    expect(LOCATIONS.some((l) => l.id === 'akuna')).toBe(true);
  });
});
