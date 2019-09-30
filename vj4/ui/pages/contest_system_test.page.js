import { NamedPage } from 'vj/misc/PageLoader';
import { LANG_MOSS_WILDCARDS } from 'vj/constant/language';

const page = new NamedPage('contest_system_test', async () => {
  const $language = $('[name="language"]');
  const $wildcards = $('[name="wildcards"]');

  const changeWildcards = () => {
    const lang = $language.val();
    const wildcards = LANG_MOSS_WILDCARDS[lang] || [];
    $wildcards.val(wildcards.join(', '));
  };

  $language.on('change', changeWildcards);
  changeWildcards();
});

export default page;
