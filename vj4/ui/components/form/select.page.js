import { AutoloadPage } from 'vj/misc/PageLoader';
import 'select2';

const selectPage = new AutoloadPage('selectPage', () => {
  console.log(1);
});

export default selectPage;
