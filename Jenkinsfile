pipeline {
  agent any
  stages {
    stage('Preparation') {
      steps {
        git 'https://github.com/joint-online-judge/cb4'
      }
    }
    stage('Build') {
      steps {
        sh 'docker build -t jioj/cb4:latest .'
      }
    }
  }
}